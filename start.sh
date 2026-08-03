#!/bin/bash
# سكربت الإقلاع: يشغّل خادم vLLM (منفذ داخلي، بالخلفية — افتراضياً 18001،
# انظر VLLM_PORT أدناه) + FastAPI (منفذ 8000، هو الوحيد المكشوف من RunPod).
#
# مصمَّم لصورة vllm/vllm-openai:gemma4-unified (انظر Dockerfile) — الصورة
# الوحيدة حالياً بدعم Gemma4ForConditionalGeneration (PR #44429، ما صدر بعد
# بإصدار مستقر). إذا شغّلته على Pod بقالب PyTorch عام (بدون الصورة الرسمية)،
# السكربت يكتشف فشل الإقلاع تلقائياً ويصلحه (نسخة vllm غير مدعومة، أو مسار
# مكتبات CUDA runtime ناقص) بدل التخمين المسبق — شوف _start_vllm أدناه.
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
#
# نسختان من نفس الموديل بالتوازي على نفس الكارت (VLLM_REPLICAS، افتراضياً 2):
# كل نسخة عملية vllm serve منفصلة على منفذ مستقل (VLLM_PORT, VLLM_PORT+1, ...)،
# وتحمّل GPU_MEMORY_UTILIZATION تُقسَّم عليهن بالتساوي (كل نسخة تاخذ حصتها
# فقط من الكارت، والمجموع = القيمة الأصلية). الفائدة: عمليتا vllm منفصلتان =
# جدولة/تزامن مستقلان، فطلب طويل بنسخة وحدة ما يؤخر طابور النسخة الثانية —
# مقابل ذلك KV cache أصغر لكل نسخة (نص الحصة تقريباً) فـ MAX_NUM_SEQS الفعّال
# لكل نسخة أقل، لكن المجموع بين النسختين يقارب نفس السعة الكلية. الباك اند
# (app/engine.py) يوزّع الطلبات بينهن بـ round-robin مع تتبّع جاهزية مستقلة
# لكل نسخة — انظر VLLM_BASE_URLS بـ.env.example.

set -e
cd "$(dirname "$0")"

pip install --no-cache-dir -q -r requirements.txt -r requirements-gpu.txt
command -v ffmpeg >/dev/null 2>&1 || {
    echo "==> تثبيت ffmpeg (لازم لميزة تحويل الصوت لنص)..."
    apt-get update -qq && apt-get install -y -qq --no-install-recommends ffmpeg
}
# libsndfile1 لازمة لـ soundfile (app/features/voice_followup/tts.py) — كتابة
# مخرَج F5-TTS كملف WAV. ldconfig -p يتحقق من وجود مكتبة النظام نفسها (الحزمة
# pip منفصلة عن مكتبة C اللي تربطها)، لا حزمة python soundfile.
ldconfig -p 2>/dev/null | grep -q libsndfile.so || {
    echo "==> تثبيت libsndfile1 (لازمة لميزة تحويل النص لصوت)..."
    apt-get update -qq && apt-get install -y -qq --no-install-recommends libsndfile1
}

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
# عدد نسخ vLLM بالتوازي على نفس الكارت — كل نسخة منفذها VLLM_PORT+i ونصيبها
# من GPU_MEMORY_UTILIZATION/VLLM_REPLICAS ومن MAX_NUM_SEQS/VLLM_REPLICAS.
VLLM_REPLICAS="${VLLM_REPLICAS:-2}"
VLLM_LOG_PREFIX="/tmp/vllm_boot"

# نسبة VRAM ونصيب الطلبات المتزامنة لكل نسخة — bc غير مضمون بكل صورة، فنستخدم
# awk (متوفر دائماً). REPLICA_GPU_MEM بدقة 4 خانات عشرية (كافية لعلَم vLLM).
REPLICA_GPU_MEM=$(awk -v u="${GPU_MEMORY_UTILIZATION}" -v n="${VLLM_REPLICAS}" 'BEGIN { printf "%.4f", u / n }')
REPLICA_MAX_NUM_SEQS=$(( MAX_NUM_SEQS / VLLM_REPLICAS ))

# تنظيف عمليات لنا عالقة من تشغيلة سابقة (start.sh انقطع بمنتصف الطريق أو
# أُعيد تشغيله يدوياً بنفس الجلسة)، ثم تحقق أن كل منافذ النسخ فاضية فعلاً —
# لو لسا محجوزة فمن خدمة نظام (لاحظنا nginx داخلي ماسك 8001 على بعض قوالب
# RunPod العامة) وليس عملية vllm سابقة لنا، فنفشل بخطأ واضح فوراً بدل تكرار
# "Address already in use" الغامض من داخل vLLM.
_port_owner() {
    ss -ltnp 2>/dev/null | grep ":$1 " | sed -n 's/.*users:(("\([^"]*\)".*/\1/p' | head -1
}
pkill -9 -f "vllm.entrypoints.openai.api_server" 2>/dev/null || true
pkill -9 -f "uvicorn app.main:app" 2>/dev/null || true
sleep 1
for i in $(seq 0 $((VLLM_REPLICAS - 1))); do
    port=$((VLLM_PORT + i))
    owner=$(_port_owner "${port}")
    if [ -n "${owner}" ]; then
        echo "🛑 المنفذ ${port} محجوز فعلاً من عملية نظام: ${owner} (مو خادم vLLM سابق لنا)."
        echo "   غيّر VLLM_PORT لمنفذ يبدأ به التسلسل، مثلاً: VLLM_PORT=18101 bash start.sh"
        exit 1
    fi
done

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
    # $1=منفذ $2=ملف اللوق — يخلّف العملية بالخلفية بالشل الحالي نفسه (لا
    # command substitution: subshell $(...) يفقد ملكية العملية الخلفية بمجرد
    # خروجه ببعض بيئات bash، فيوصل PID لعملية ميتة بالشل الأم). النتيجة تُقرأ
    # مباشرة من $! بعد الاستدعاء عبر المتغير العالمي VLLM_LAST_PID.
    python3 -m vllm.entrypoints.openai.api_server \
        --model "${MODEL_NAME}" \
        --host 127.0.0.1 \
        --port "$1" \
        --max-model-len "${MAX_MODEL_LEN}" \
        --gpu-memory-utilization "${REPLICA_GPU_MEM}" \
        --max-num-seqs "${REPLICA_MAX_NUM_SEQS}" \
        --async-scheduling \
        --enable-prefix-caching \
        --limit-mm-per-prompt '{"image": 1, "audio": 0}' \
        > "$2" 2>&1 &
    VLLM_LAST_PID=$!
}

VLLM_PIDS=()
VLLM_LOGS=()
for i in $(seq 0 $((VLLM_REPLICAS - 1))); do
    port=$((VLLM_PORT + i))
    log="${VLLM_LOG_PREFIX}_${i}.log"
    VLLM_LOGS+=("${log}")
    echo "==> تشغيل نسخة vLLM رقم ${i} على المنفذ ${port} (حصة GPU ${REPLICA_GPU_MEM}, الموديل: ${MODEL_NAME})..."
    _start_vllm "${port}" "${log}"
    VLLM_PIDS+=("${VLLM_LAST_PID}")
done

# ننتظر شوي ونتأكد أن كل نسخة ما ماتت فوراً (فشل استيراد/تحميل مبكر) قبل ما
# نكمل — أخطاء لاحقة (OOM أثناء تحميل الأوزان) تبقى تظهر باللوق الخاص فيها لاحقاً.
sleep 8
for i in $(seq 0 $((VLLM_REPLICAS - 1))); do
    port=$((VLLM_PORT + i))
    log="${VLLM_LOGS[$i]}"
    pid="${VLLM_PIDS[$i]}"
    if ! kill -0 "${pid}" 2>/dev/null; then
        echo "🛑 نسخة vLLM رقم ${i} (منفذ ${port}) انطفت فوراً — سجل الإقلاع:"
        tail -n 40 "${log}"
        if grep -qi "Gemma4ForConditionalGeneration\|is not supported for now\|ValueError: Model architectures" "${log}"; then
            echo "==> السبب: نسخة vllm الحالية لا تدعم معمارية Gemma4 — تثبيت nightly wheel..."
            pip install -U vllm --pre \
                --extra-index-url https://wheels.vllm.ai/nightly/cu129 \
                --extra-index-url https://download.pytorch.org/whl/cu129 \
                --index-strategy unsafe-best-match
        elif grep -qi "libcudart\|libcublas\|cannot open shared object file" "${log}"; then
            echo "==> السبب: مسار مكتبات CUDA runtime ناقص — إصلاح LD_LIBRARY_PATH..."
            _fix_cuda_lib_path
        else
            echo "🛑 سبب غير معروف — راجع السجل أعلاه يدوياً. إيقاف."
            exit 1
        fi
        echo "==> إعادة محاولة تشغيل نسخة vLLM رقم ${i}..."
        _start_vllm "${port}" "${log}"
        pid="${VLLM_LAST_PID}"
        VLLM_PIDS[$i]="${pid}"
        sleep 8
        if ! kill -0 "${pid}" 2>/dev/null; then
            echo "🛑 فشلت المحاولة الثانية أيضاً (نسخة ${i}) — سجل الإقلاع:"
            tail -n 40 "${log}"
            exit 1
        fi
    fi
    echo "==> نسخة vLLM رقم ${i} شغّالة (PID ${pid}) على المنفذ ${port}، تكمل تحميل الأوزان بالخلفية — راقب ${log}"
done

# لو انطفت أي نسخة ينطفي الـ container كامل (أوضح من خدمة نص ميتة جزئياً)
trap 'for p in "${VLLM_PIDS[@]}"; do kill "$p" 2>/dev/null || true; done' EXIT

echo "==> تشغيل FastAPI على المنفذ ${API_PORT}..."
echo "    (الميزات تشتغل فوراً بوضع fallback؛ فاحص الجاهزية بـ app/engine.py"
echo "     يقلبها تلقائياً لوضع الموديل أول ما يكمل vLLM تحميل الأوزان)"
exec uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT}" --workers 1
