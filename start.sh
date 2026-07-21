#!/bin/bash
# سكربت الإقلاع: يشغّل خادم vLLM (منفذ 8001، بالخلفية) + FastAPI (منفذ 8000).
#
# مصمَّم لصورة vllm/vllm-openai:gemma4-unified (انظر Dockerfile) — الصورة
# الوحيدة حالياً بدعم Gemma4ForConditionalGeneration (PR #44429، ما صدر بعد
# بإصدار مستقر). إذا شغّلته على Pod بقالب آخر تأكد أن vllm بنسخة تدعم Gemma 4
# (nightly wheel):
#   pip install -U vllm --pre --extra-index-url https://wheels.vllm.ai/nightly/cu129
#
# الأعلام حسب وصفة vLLM الرسمية لـ Gemma 4 (قسم Full-Featured Server Launch)،
# مضبوطة لهدفنا: أقصى عدد طلبات متزامنة على A40 48GB بردود قصيرة —
#   --max-model-len قصير = KV cache يتسع لطلبات متزامنة أكثر
#   --async-scheduling يحسّن الـ throughput (توصية الوصفة)
#   --limit-mm-per-prompt: صورة وحدة (order_intake)، بلا صوت (الصوت عبر Whisper
#     داخل FastAPI، ما يمر بـ vLLM)

set -e
cd "$(dirname "$0")"

# --- تثبيت المتطلبات (idempotent — سريع إذا كلشي مثبَّت مسبقاً) ---
# بصورة vllm/vllm-openai:gemma4-unified (الـ Dockerfile) vllm موجود مسبقاً.
# على Pod بقالب آخر (تشغيل يدوي بدون الصورة الرسمية) لازم nightly wheel
# لأن دعم Gemma 4 ما صدر بعد بإصدار مستقر.
pip install --no-cache-dir -q -r requirements.txt -r requirements-gpu.txt
if ! python3 -c "import vllm" 2>/dev/null; then
    echo "==> vllm غير مثبَّت — تثبيت nightly wheel (دعم Gemma 4 ما صدر بإصدار مستقر بعد)..."
    pip install -U vllm --pre \
        --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
        --extra-index-url https://download.pytorch.org/whl/cu129
fi
command -v ffmpeg >/dev/null 2>&1 || {
    echo "==> تثبيت ffmpeg (لازم لميزة تحويل الصوت لنص)..."
    apt-get update -qq && apt-get install -y -qq --no-install-recommends ffmpeg
}

MODEL_NAME="${MODEL_NAME:-ameer4wisam/gemma-iraqi-finetune-v2}"
VLLM_PORT="${VLLM_PORT:-8001}"
API_PORT="${API_PORT:-8000}"
MAX_MODEL_LEN="${MAX_MODEL_LEN:-4096}"
GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.90}"

echo "==> تشغيل خادم vLLM على المنفذ ${VLLM_PORT} (الموديل: ${MODEL_NAME})..."
python3 -m vllm.entrypoints.openai.api_server \
    --model "${MODEL_NAME}" \
    --host 127.0.0.1 \
    --port "${VLLM_PORT}" \
    --max-model-len "${MAX_MODEL_LEN}" \
    --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" \
    --async-scheduling \
    --limit-mm-per-prompt '{"image": 1, "audio": 0}' \
    &
VLLM_PID=$!

# لو انطفى أي من العمليتين ينطفي الـ container كامل (أوضح من خدمة نص ميتة)
trap 'kill ${VLLM_PID} 2>/dev/null || true' EXIT

echo "==> تشغيل FastAPI على المنفذ ${API_PORT}..."
echo "    (الميزات تشتغل فوراً بوضع fallback؛ فاحص الجاهزية بـ app/engine.py"
echo "     يقلبها تلقائياً لوضع الموديل أول ما يكمل vLLM تحميل الأوزان)"
exec uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT}" --workers 1
