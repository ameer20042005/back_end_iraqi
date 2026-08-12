# ============================================================================
# back_end_iraqi — صورة FastAPI + خادم vLLM (Gemma 4 عراقي) بحاوية واحدة
# ============================================================================
# القاعدة: خادم vLLM الرسمي مع دعم Gemma 4 (معمارية Gemma4ForConditionalGeneration
# وصلت عبر PR #44429 — متوفرة بهذه الصورة المثبَّتة تحديداً، ولم تصدر بعد
# بإصدار مستقر). حسب وصفة vLLM الرسمية لـ Gemma 4.
# على مضيف CUDA 12.9 مرّر: --build-arg BASE_IMAGE=vllm/vllm-openai:gemma4-unified-cu129
#
# ⚠️ هذا البناء (docker build) لا يُنزّل أي وزن موديل إطلاقاً — لا Gemma
# (~24GB) ولا Whisper ولا F5-TTS. كل ما يحدث وقت البناء: سحب الصورة الأساسية
# (تحتوي vllm/torch/transformers/CUDA جاهزة مسبقاً) + تثبيت حزم Python خفيفة
# (بضع عشرات الميغابايت) + نسخ كود المشروع النصي. الأوزان الفعلية تتنزّل
# فقط عند **أول تشغيل** حقيقي للحاوية على مضيف GPU (start.sh يستدعي
# `vllm serve` الذي يجلب الموديل من Hugging Face Hub) — لا علاقة لها بالبناء
# إطلاقاً. لذلك يمكن تنفيذ `docker build` على أي جهاز حتى بلا GPU وباتصال
# متواضع (السحب الثقيل الوحيد هنا هو الصورة الأساسية نفسها، مرة واحدة فقط
# ثم تُخزَّن بكاش Docker المحلي).
ARG BASE_IMAGE=vllm/vllm-openai:gemma4-unified
FROM ${BASE_IMAGE}

WORKDIR /workspace/app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg لازم لتحويل الصوت لنص (app/features/order_intake/transcribe.py).
# libsndfile1 لازمة لـ soundfile (app/features/voice_followup/tts.py) —
# كتابة مخرَج F5-TTS كملف WAV. curl فقط لفحص HEALTHCHECK أدناه.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

# متطلبات Python بطبقة منفصلة عن كود المشروع (COPY . . أدناه) حتى يستفيد
# كاش Docker: أي تعديل بالكود لا يعيد تثبيت الحزم من الصفر، فقط تغيير
# requirements*.txt يعيد هذي الطبقة.
#
# requirements.txt      — عميل FastAPI/httpx الرفيع (vllm/torch/transformers
#                          الأساسية موجودة مسبقاً بالصورة).
# requirements-gpu.txt   — مكتبات ميزات الصوت/الصورة (transformers بسقف نسخة
#                          صارم عمداً — انظر تعليق الملف نفسه لسبب الأزمة
#                          السابقة، Pillow، f5-tts، soundfile، huggingface_hub).
# تثبيتها هنا بدل الاعتماد فقط على pip install وقت start.sh (كما كانت سابقاً)
# يجعل إقلاع الحاوية أسرع وأوثق (لا حاجة اتصال إنترنت لتثبيت حزم عند كل
# إعادة تشغيل) — start.sh يبقى يستدعي نفس أمر pip احتياطاً (يتحقق أن كل شي
# مثبَّت فعلاً، ويبقى ضرورياً بمسار "Pod مباشر" بدون هذي الصورة المخصصة —
# انظر RUNPOD_DEPLOY.md الطريقة 1)، لكنه يمر بسرعة (لا شيء لتثبيته فعلياً).
# **لا شيء بهذي الخطوة يُنزّل وزن موديل** — فقط مكتبات بايثون؛ التنزيل
# الفعلي للأوزان lazy تماماً عند أول استخدام وقت التشغيل (warmup بـ app/main.py).
COPY requirements.txt requirements-gpu.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-gpu.txt

COPY . .
RUN chmod +x start.sh

# 8000: FastAPI (الواجهة العامة، الوحيدة المكشوفة من RunPod).
# منفذ (منافذ) خادم vLLM الداخلي (18001+ افتراضياً، انظر VLLM_PORT بـ start.sh)
# اتصال داخلي بين عمليتين بنفس الحاوية فقط — لا يحتاج EXPOSE.
EXPOSE 8000

# فحص صحة الحاوية — /health يرجع فوراً حتى لو خادم vLLM لسا يحمّل الأوزان
# (الميزات بوضع fallback مؤقتاً)، فهذا يتحقق فقط أن عملية FastAPI حية، لا أن
# الموديل جاهز فعلاً (لمعرفة ذلك استخدم GET /gpu → "vllm_ready": true).
# start-period طويلة نسبياً لأن start.sh يشغّل vLLM أولاً وقد يأخذ دقائق
# بتحميل الأوزان أول مرة قبل ما يصل CMD أصلاً لسطر uvicorn.
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# صورة vLLM الأصلية ENTRYPOINT مالها يشغّل `vllm serve` مباشرة — نلغيه حتى
# يشتغل start.sh (يشغّل خادم(ات) vLLM بالخلفية + uvicorn بالمقدمة).
ENTRYPOINT []
CMD ["bash", "/workspace/app/start.sh"]
