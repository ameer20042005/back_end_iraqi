#!/bin/bash
# سكربت الإقلاع: يشغّل خادم vLLM (منفذ داخلي، بالخلفية — افتراضياً 18001،
# انظر VLLM_PORT أدناه) + FastAPI (منفذ 8000، هو الوحيد المكشوف من RunPod).
#
# مصمَّم للعمل على قالب RunPod **Ubuntu 22.04 خام** (بدون Python ولا pip
# ولا CUDA ولا PyTorch ولا vLLM مسبقاً — انظر RUNPOD_DEPLOY.md، الطريقة
# الوحيدة المعتمدة للنشر): قسم "تجهيز نظام التشغيل" بالأسفل يثبّت كل شي
# لازم من الصفر بأول إقلاع (Python/pip، أدوات بناء، ffmpeg/libsndfile1)،
# ثم يتحقق من توفر vLLM بدعم معمارية Gemma4ForConditionalGeneration
# (PR #44429، ما صدر بعد بإصدار مستقر) ويثبّت نسخة nightly تلقائياً لو
# ناقصة أو غير مدعومة — شوف _start_vllm أدناه. يشتغل بنفس الموثوقية لو
# استُخدمت صورة جاهزة فيها بعض هذي المكونات مسبقاً (مثل vllm/vllm-openai
# أو قالب PyTorch عام) — كل خطوة تتخطى نفسها لو لقت الأداة موجودة أصلاً.
#
# الأعلام حسب وصفة vLLM الرسمية لـ Gemma 4 (قسم Full-Featured Server Launch)،
# مضبوطة لهدفنا: أقصى عدد طلبات متزامنة على A100/H100 80GB+ بردود قصيرة —
#   --max-model-len قصير = KV cache يتسع لطلبات متزامنة أكثر
#   --async-scheduling يحسّن الـ throughput (توصية الوصفة)
#   --enable-prefix-caching أكبر مكسب سرعة بجهة الخادم عندنا: كل الطلبات
#     تشترك بنفس البادئة الضخمة (system prompt الكامل من plane.md + مرجع
#     المحافظات)، وبدونه تُعاد معالجة آلاف التوكنات المتطابقة بكل طلب. مع
#     التفعيل تُحسب مرة وتُعاد من الكاش، فينزل زمن ما-قبل-التوليد (prefill)
#     بشدة — وهو الجزء الغالب من زمن الاستجابة بردودنا القصيرة.
#   --limit-mm-per-prompt: صورة وحدة (order_intake)، بلا صوت (الصوت عبر Whisper
#     داخل FastAPI، ما يمر بـ vLLM)
#
# MAX_NUM_SEQS محسوبة على A100/H100 80GB (moved من A40 48GB): الأوزان
# (bf16, 12B) تأخذ ~24.4GB من أصل ~72GB متاحة (90% من 80GB)، فيتبقى ~47.6GB
# لـ KV cache — مقابل ~17GB على A40. بنفس MAX_MODEL_LEN=10000 (٠.375MB/توكن
# لكل طلب × 10000 ≈ 3.7GB/طلب)، هذا يتسع لـ ~12 طلباً متزامناً بالسياق
# الأقصى فعلياً، لكن أغلب المحادثات أقصر بكثير من الأقصى فتتقاسم عدة طلبات
# نفس صفحات الـ KV cache — عملياً ~350 طلباً متزامناً بأحمال الإنتاج
# الفعلية (نفس منطق --max-num-seqs بوصفة vLLM: سقف الجدولة لا حجزاً ثابتاً).

set -e
cd "$(dirname "$0")"

# ── تجهيز نظام التشغيل من الصفر (Ubuntu 22.04 خام) ──────────────────────────
# بدون صورة جاهزة فيها Python/pip أصلاً، لازم نثبّتهم قبل أي أمر pip/python3
# بهذا السكربت. الإصدارات أدناه مثبَّتة (=) حسب أرشيف Ubuntu 22.04 (jammy)
# الحالي وقت كتابة هذا الملف؛ لو الصورة تستخدم مرآة apt بنسخة مختلفة (أقدم/
# أحدث) ما تلقى بالضبط هذا الرقم، فـ_apt_install_pinned يتراجع تلقائياً
# لأحدث نسخة متوفرة بدل ما يفشل الإقلاع بالكامل بسبب رقم نسخة apt غير موجود.
_apt_install_pinned() {
    # $1=اسم الحزمة $2=الإصدار المثبَّت
    local pkg="$1" ver="$2"
    dpkg -s "${pkg}" >/dev/null 2>&1 && return 0
    apt-get install -y -qq --no-install-recommends "${pkg}=${ver}" 2>/dev/null || {
        echo "==> Exact pin ${pkg}=${ver} not found on this image's apt mirror — installing latest available instead"
        apt-get install -y -qq --no-install-recommends "${pkg}"
    }
}

if ! command -v python3 >/dev/null 2>&1 || ! python3 -m pip --version >/dev/null 2>&1; then
    echo "==> Bootstrapping base OS packages (bare Ubuntu 22.04 image — no Python/pip yet)..."
    apt-get update -qq
    _apt_install_pinned python3         3.10.6-1~22.04
    _apt_install_pinned python3-pip     22.0.2+dfsg-1ubuntu0.7
    _apt_install_pinned python3-dev     3.10.6-1~22.04
    # build-essential/git: بعض حزم requirements-gpu.txt (مثل encodec، تبعية
    # f5-tts) تُوزَّع sdist بلا wheel جاهزة فتحتاج مترجم C وقت التثبيت.
    _apt_install_pinned build-essential 12.9ubuntu3
    _apt_install_pinned git             1:2.34.1-1ubuntu1.17
fi

python3 -m pip install --no-cache-dir -q -r requirements.txt -r requirements-gpu.txt
command -v ffmpeg >/dev/null 2>&1 || {
    echo "==> Installing ffmpeg (required for the speech-to-text feature)..."
    apt-get update -qq
    _apt_install_pinned ffmpeg 7:4.4.2-0ubuntu0.22.04.1
}
# libsndfile1 لازمة لـ soundfile (app/features/voice_followup/tts.py) — كتابة
# مخرَج F5-TTS كملف WAV. ldconfig -p يتحقق من وجود مكتبة النظام نفسها (الحزمة
# pip منفصلة عن مكتبة C اللي تربطها)، لا حزمة python soundfile.
ldconfig -p 2>/dev/null | grep -q libsndfile.so || {
    echo "==> Installing libsndfile1 (required for the text-to-speech feature)..."
    apt-get update -qq
    _apt_install_pinned libsndfile1 1.0.31-2ubuntu0.2
}

# فحص مبكر: هل vLLM (بدعم Gemma4) موجود أصلاً؟ على Ubuntu خام الجواب دائماً
# لأ (ماكو vLLM إطلاقاً)، فنثبّت نسخة nightly مباشرة بدل الانتظار لمحاولة
# إقلاع فاشلة أولاً (تخمين مسبق مقصود هنا، بعكس بقية السكربت — الفحص نفسه
# رخيص وأسرع من محاولة تشغيل فعلية). حلقة _start_vllm بالأسفل تبقى شبكة أمان
# ثانية لو الفحص أعطى نتيجة خاطئة (مثلاً vLLM مستورد لكن معماريته لا تدعم
# Gemma4 فعلياً — لا يظهر إلا وقت التشغيل الحقيقي).
if ! python3 -c "import vllm.entrypoints.openai.api_server" 2>/dev/null; then
    echo "==> vLLM (with Gemma4 support) not found — installing the nightly wheel before first launch..."
    python3 -m pip install -U vllm --pre \
        --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
        --extra-index-url https://download.pytorch.org/whl/cu129
fi

# 8001 مستخدَم أحياناً من خدمة نظام على قوالب RunPod العامة (لاحظنا nginx
# داخلي ماسكه بالفعل على بعض الـ Pods) — 18001 منفذ داخلي (بين الحاويتين
# فقط، ما يحتاج Expose من لوحة RunPod) أقل عرضة للتصادم. غيّره بـ VLLM_PORT
# لو لسا يتصادم بمنفذك.
MODEL_NAME="${MODEL_NAME:-ameer4wisam/gemma-iraqi-finetune-v2}"
VLLM_PORT="${VLLM_PORT:-18001}"
API_PORT="${API_PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-10000}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
MAX_NUM_SEQS="${MAX_NUM_SEQS:-350}"
VLLM_LOG="/tmp/vllm_boot.log"

# تنظيف عمليات لنا عالقة من تشغيلة سابقة (start.sh انقطع بمنتصف الطريق أو
# أُعيد تشغيله يدوياً بنفس الجلسة)، ثم تحقق أن منفذ vLLM فاضي فعلاً — لو لسا
# محجوز فمن خدمة نظام (لاحظنا nginx داخلي ماسك 8001 على بعض قوالب RunPod
# العامة) وليس عملية vllm سابقة لنا، فنفشل بخطأ واضح فوراً بدل تكرار
# "Address already in use" الغامض من داخل vLLM.
_port_owner() {
    ss -ltnp 2>/dev/null | grep ":$1 " | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -1
}
pkill -9 -f "vllm.entrypoints.openai.api_server" 2>/dev/null || true
pkill -9 -f "uvicorn app.main:app" 2>/dev/null || true
sleep 1
owner=$(_port_owner "${VLLM_PORT}")
if [ -n "${owner}" ]; then
    echo "🛑 Port ${VLLM_PORT} is already held by a system process: ${owner} (not one of our previous vLLM servers)."
    echo "   Change VLLM_PORT to a different port, e.g.: VLLM_PORT=18101 bash start.sh"
    exit 1
fi

_fix_cuda_lib_path() {
    # مكتبات CUDA runtime قد تُثبَّت كحزم pip (nvidia-cuda-runtime-cu13...)
    # بدون إضافة مسارها لـ loader path — شائع على Ubuntu خام وقوالب PyTorch
    # العامة (غير صورة vllm/vllm-openai الرسمية)، ويظهر كـ "libcudart.so.13: cannot open
    # shared object file" رغم أن الملف موجود فعلاً بمجلد site-packages، عادة
    # بمسار عميق مثل .../dist-packages/nvidia/cu13/lib (8+ مستويات من /).
    # نسأل Python نفسه أين مثبَّتة حزمة nvidia بدل find من الجذر (أسرع، وما
    # يعتمد على تخمين maxdepth صحيح).
    local nvidia_pkg_dir dirs
    nvidia_pkg_dir=$(python3 -c "import nvidia, os; print(os.path.dirname(nvidia.__path__[0]))" 2>/dev/null)
    if [ -z "${nvidia_pkg_dir}" ] || [ ! -d "${nvidia_pkg_dir}" ]; then
        echo "==> nvidia pip package not found — skipping CUDA path fix"
        return
    fi
    dirs=$(find "${nvidia_pkg_dir}/nvidia" -maxdepth 2 -type d -name lib 2>/dev/null | paste -sd: -)
    if [ -n "${dirs}" ]; then
        export LD_LIBRARY_PATH="${dirs}:${LD_LIBRARY_PATH}"
        echo "==> Added CUDA library paths from the nvidia pip packages to LD_LIBRARY_PATH:"
        echo "    ${dirs}"
    else
        echo "==> No lib directories found under ${nvidia_pkg_dir}/nvidia — this fix doesn't apply here"
    fi
}

_start_vllm() {
    # يخلّف العملية بالخلفية بالشل الحالي نفسه (لا command substitution:
    # subshell $(...) يفقد ملكية العملية الخلفية بمجرد خروجه ببعض بيئات
    # bash، فيوصل PID لعملية ميتة بالشل الأم). النتيجة تُقرأ مباشرة من $!
    # بعد الاستدعاء عبر المتغير العالمي VLLM_LAST_PID.
    python3 -m vllm.entrypoints.openai.api_server \
        --model "${MODEL_NAME}" \
        --host 127.0.0.1 \
        --port "${VLLM_PORT}" \
        --max-model-len "${MAX_MODEL_LEN}" \
        --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
        --max-num-seqs "${MAX_NUM_SEQS}" \
        --async-scheduling \
        --enable-prefix-caching \
        --limit-mm-per-prompt '{"image": 1, "audio": 0}' \
        > "${VLLM_LOG}" 2>&1 &
    VLLM_LAST_PID=$!
}

echo "==> Starting vLLM on port ${VLLM_PORT} (model: ${MODEL_NAME})..."
_start_vllm
VLLM_PID=${VLLM_LAST_PID}

# ننتظر شوي ونتأكد أن العملية ما ماتت فوراً (فشل استيراد/تحميل مبكر) قبل ما
# نكمل — أخطاء لاحقة (OOM أثناء تحميل الأوزان) تبقى تظهر باللوق لاحقاً.
sleep 8
if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
    echo "🛑 vLLM died immediately — boot log:"
    tail -n 40 "${VLLM_LOG}"
    if grep -qi "Gemma4ForConditionalGeneration\|is not supported for now\|ValueError: Model architectures\|ModuleNotFoundError\|No module named" "${VLLM_LOG}"; then
        echo "==> Cause: the current vllm version doesn't support the Gemma4 architecture (or isn't installed) — installing nightly wheel..."
        python3 -m pip install -U vllm --pre \
            --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
            --extra-index-url https://download.pytorch.org/whl/cu129
    elif grep -qi "libcudart\|libcublas\|cannot open shared object file" "${VLLM_LOG}"; then
        echo "==> Cause: CUDA runtime library path is missing — fixing LD_LIBRARY_PATH..."
        _fix_cuda_lib_path
    else
        echo "🛑 Unknown cause — check the log above manually. Stopping."
        exit 1
    fi
    echo "==> Retrying vLLM startup..."
    _start_vllm
    VLLM_PID=${VLLM_LAST_PID}
    sleep 8
    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
        echo "🛑 Second attempt also failed — boot log:"
        tail -n 40 "${VLLM_LOG}"
        exit 1
    fi
fi
echo "==> vLLM is running (PID ${VLLM_PID}) on port ${VLLM_PORT}, still loading weights in the background — watch ${VLLM_LOG}"

# لو انطفت عملية vLLM ينطفي الـ container كامل (أوضح من خدمة نص ميتة جزئياً)
trap 'kill "${VLLM_PID}" 2>/dev/null || true' EXIT

echo "==> Starting FastAPI on port ${API_PORT}..."
echo "    (features run immediately in fallback mode; the readiness check in app/engine.py"
echo "     switches them to model mode automatically once vLLM finishes loading weights)"
exec uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT}" --workers 1
