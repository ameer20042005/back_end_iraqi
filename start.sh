#!/bin/bash
# سكربت الإقلاع: يشغّل خادم vLLM (منفذ 8001، بالخلفية) + FastAPI (منفذ 8000).
#
# مصمَّم لصورة vllm/vllm-openai:gemma4-unified (انظر Dockerfile) — الصورة
# الوحيدة حالياً بدعم Gemma4ForConditionalGeneration (PR #44429، ما صدر بعد
# بإصدار مستقر). إذا شغّلته على Pod بقالب PyTorch عام (بدون الصورة الرسمية)،
# السكربت يكتشف فشل الإقلاع تلقائياً ويصلحه (نسخة vllm غير مدعومة، أو مسار
# مكتبات CUDA runtime ناقص) بدل التخمين المسبق — شوف _start_vllm أدناه.
#
# الأعلام حسب وصفة vLLM الرسمية لـ Gemma 4 (قسم Full-Featured Server Launch)،
# مضبوطة لهدفنا: أقصى عدد طلبات متزامنة على A40 48GB بردود قصيرة —
#   --max-model-len قصير = KV cache يتسع لطلبات متزامنة أكثر
#   --async-scheduling يحسّن الـ throughput (توصية الوصفة)
#   --limit-mm-per-prompt: صورة وحدة (order_intake)، بلا صوت (الصوت عبر Whisper
#     داخل FastAPI، ما يمر بـ vLLM)

set -e
cd "$(dirname "$0")"

pip install --no-cache-dir -q -r requirements.txt -r requirements-gpu.txt
command -v ffmpeg >/dev/null 2>&1 || {
    echo "==> تثبيت ffmpeg (لازم لميزة تحويل الصوت لنص)..."
    apt-get update -qq && apt-get install -y -qq --no-install-recommends ffmpeg
}

MODEL_NAME="${MODEL_NAME:-ameer4wisam/gemma-iraqi-finetune-v2}"
VLLM_PORT="${VLLM_PORT:-8001}"
API_PORT="${API_PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"
VLLM_LOG="/tmp/vllm_boot.log"

_fix_cuda_lib_path() {
    # مكتبات CUDA runtime قد تُثبَّت كحزم pip (nvidia-cuda-runtime-cu13...)
    # بدون إضافة مسارها لـ loader path — شائع بقوالب PyTorch العامة (غير
    # صورة vllm/vllm-openai الرسمية)، ويظهر كـ "libcudart.so.13: cannot open
    # shared object file" رغم أن الملف موجود فعلاً بمجلد site-packages، عادة
    # بمسار عميق مثل .../dist-packages/nvidia/cu13/lib (8+ مستويات من /).
    # نسأل Python نفسه أين مثبَّتة حزمة nvidia بدل find من الجذر (أسرع، وما
    # يعتمد على تخمين maxdepth صحيح).
    local nvidia_pkg_dir dirs
    nvidia_pkg_dir=$(python3 -c "import nvidia, os; print(os.path.dirname(nvidia.__path__[0]))" 2>/dev/null)
    if [ -z "${nvidia_pkg_dir}" ] || [ ! -d "${nvidia_pkg_dir}" ]; then
        echo "==> حزمة nvidia (pip) غير موجودة — تخطي إصلاح مسار CUDA"
        return
    fi
    dirs=$(find "${nvidia_pkg_dir}/nvidia" -maxdepth 2 -type d -name lib 2>/dev/null | paste -sd: -)
    if [ -n "${dirs}" ]; then
        export LD_LIBRARY_PATH="${dirs}:${LD_LIBRARY_PATH}"
        echo "==> أُضيفت مسارات مكتبات CUDA من حزم nvidia pip إلى LD_LIBRARY_PATH:"
        echo "    ${dirs}"
    else
        echo "==> ماكو مجلدات lib تحت ${nvidia_pkg_dir}/nvidia — الإصلاح ما ينطبق هنا"
    fi
}

_start_vllm() {
    python3 -m vllm.entrypoints.openai.api_server \
        --model "${MODEL_NAME}" \
        --host 127.0.0.1 \
        --port "${VLLM_PORT}" \
        --max-model-len "${MAX_MODEL_LEN}" \
        --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
        --async-scheduling \
        --limit-mm-per-prompt '{"image": 1, "audio": 0}' \
        > "${VLLM_LOG}" 2>&1 &
    VLLM_PID=$!
}

echo "==> تشغيل خادم vLLM على المنفذ ${VLLM_PORT} (الموديل: ${MODEL_NAME})..."
_start_vllm
# ننتظر شوي ونتأكد أن العملية ما ماتت فوراً (فشل استيراد/تحميل مبكر) قبل ما
# نكمل — أخطاء لاحقة (OOM أثناء تحميل الأوزان) تبقى تظهر بـ VLLM_LOG لاحقاً.
sleep 8
if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
    echo "🛑 خادم vLLM انطفى فوراً — سجل الإقلاع:"
    tail -n 40 "${VLLM_LOG}"
    if grep -qi "Gemma4ForConditionalGeneration\|is not supported for now\|ValueError: Model architectures" "${VLLM_LOG}"; then
        echo "==> السبب: نسخة vllm الحالية لا تدعم معمارية Gemma4 — تثبيت nightly wheel..."
        pip install -U vllm --pre \
            --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
            --extra-index-url https://download.pytorch.org/whl/cu129 \
            --index-strategy unsafe-best-match
    elif grep -qi "libcudart\|libcublas\|cannot open shared object file" "${VLLM_LOG}"; then
        echo "==> السبب: مسار مكتبات CUDA runtime ناقص — إصلاح LD_LIBRARY_PATH..."
        _fix_cuda_lib_path
    else
        echo "🛑 سبب غير معروف — راجع السجل أعلاه يدوياً. إيقاف."
        exit 1
    fi
    echo "==> إعادة محاولة تشغيل خادم vLLM..."
    _start_vllm
    sleep 8
    if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
        echo "🛑 فشلت المحاولة الثانية أيضاً — سجل الإقلاع:"
        tail -n 40 "${VLLM_LOG}"
        exit 1
    fi
fi
echo "==> خادم vLLM شغّال (PID ${VLLM_PID})، يكمل تحميل الأوزان بالخلفية — راقب ${VLLM_LOG}"

# لو انطفى أي من العمليتين ينطفي الـ container كامل (أوضح من خدمة نص ميتة)
trap 'kill ${VLLM_PID} 2>/dev/null || true' EXIT

echo "==> تشغيل FastAPI على المنفذ ${API_PORT}..."
echo "    (الميزات تشتغل فوراً بوضع fallback؛ فاحص الجاهزية بـ app/engine.py"
echo "     يقلبها تلقائياً لوضع الموديل أول ما يكمل vLLM تحميل الأوزان)"
exec uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT}" --workers 1
