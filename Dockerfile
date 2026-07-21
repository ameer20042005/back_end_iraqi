# خادم vLLM الرسمي مع دعم Gemma 4 (معمارية Gemma4ForConditionalGeneration
# وصلت عبر PR #44429 — متوفرة بهذه الصورة المثبَّتة تحديداً، ولم تصدر بعد
# بإصدار مستقر). حسب وصفة vLLM الرسمية لـ Gemma 4.
# على مضيف CUDA 12.9 استخدم الوسم gemma4-unified-cu129 بدلاً منه.
FROM vllm/vllm-openai:gemma4-unified

WORKDIR /workspace/app

# ffmpeg لازم لتحويل الصوت لنص (app/features/order_intake/transcribe.py)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# متطلبات FastAPI فقط — torch/transformers/vllm موجودة مسبقاً بصورة vLLM
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 8000: FastAPI (الواجهة العامة) | 8001: خادم vLLM (داخلي)
EXPOSE 8000

# صورة vLLM الأصلية ENTRYPOINT مالها يشغّل `vllm serve` مباشرة — نلغيه حتى
# يشتغل start.sh (يشغّل خادم vLLM بالخلفية + uvicorn بالمقدمة).
ENTRYPOINT []
CMD ["bash", "/workspace/app/start.sh"]
