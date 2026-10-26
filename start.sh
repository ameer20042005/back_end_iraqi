#!/bin/bash
# vLLM + FastAPI/Pipecat, one voice process and one lazily loaded TTS instance.
# Arabic Whisper handles speech; Qwen3.5 handles dialogue. See docs/pipecat-native-audio.md for memory sizing.
# Ubuntu 22.04's Python 3.10 is upgraded into an isolated managed 3.12 environment.
set -Ee
cd "$(dirname "$0")"
export PYTHONUNBUFFERED=1
BOOT_STAGE="initializing"

_boot_stage() {
    BOOT_STAGE="$1"
    printf '[boot %s] %s\n' "$(date -u +%FT%TZ)" "${BOOT_STAGE}"
}

_startup_error() {
    local status="$1" line="$2"
    # Do not print BASH_COMMAND: commands can contain credentials.
    printf '[boot ERROR] stage=%s line=%s exit=%s\n' "${BOOT_STAGE}" "${line}" "${status}" >&2
    exit "${status}"
}
trap '_startup_error "$?" "$LINENO"' ERR

_cleanup_services() {
    local pid
    for pid in "${API_PID:-}" "${VLLM_PID:-}"; do
        if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
            kill "${pid}" 2>/dev/null || true
        fi
    done
}

_supervise_services() {
    local status
    if wait -n "${VLLM_PID}" "${API_PID}"; then
        status=0
    else
        status=$?
    fi
    printf '[boot] A service exited (exit=%s); stopping the other service. See /tmp/api.log and %s\n' \
        "${status}" "${VLLM_LOG}" >&2
    return "${status}"
}

_boot_stage "Checking system packages"

# Requirements can contain extras (uvicorn[standard]); pip constraints cannot.
# Keep every version pin while removing extras only from the temporary constraints.
# Both initial installation and recovery must use the same constraints.
_install_vllm() (
    constraints=$(mktemp)
    trap 'rm -f -- "${constraints}"' EXIT
    sed -E 's/^([[:space:]]*[[:alnum:]_.-]+)\[[^]]+\]/\1/' \
        requirements.txt requirements-gpu.txt > "${constraints}"
    # Constraints alone do not include Pipecat's dependency graph. Resolve the
    # app requirements alongside vLLM so shared packages (e.g. protobuf) remain
    # compatible instead of silently replacing them with incompatible releases.
    # Reuse satisfied packages, including large CUDA wheels. A failed import is
    # not evidence that the entire environment needs reinstalling.
    # vLLM/torch come from PyPI at the exact versions in requirements-vllm.txt.
    # The nightly index changed on every rebuild; PyPI releases do not.
    python3 -m pip install --no-cache-dir --progress-bar off -c "${constraints}" \
        -r requirements-vllm.txt -r requirements.txt -r requirements-gpu.txt
)

_ensure_python_dependencies() {
    _boot_stage "Checking installed Python requirements (offline)"
    if python3 scripts/check_requirements.py requirements-vllm.txt requirements.txt requirements-gpu.txt; then
        echo "==> Python requirements already satisfied — skipping installation"
    else
        _boot_stage "Installing missing or incompatible Python requirements"
        _install_vllm
        # Do not launch a half-installed environment after a resolver warning.
        python3 scripts/check_requirements.py requirements-vllm.txt requirements.txt requirements-gpu.txt
    fi
}

_prepare_model_cache() {
    # Set these before any transformers/huggingface_hub import. /workspace is
    # the RunPod volume; completed model shards must survive container restarts.
    local old_cache cache_parent
    if [ -z "${HF_HOME:-}" ]; then
        old_cache="${XDG_CACHE_HOME:-${HOME}/.cache}/huggingface"
        cache_parent="${PWD}/.cache"
        if [ -d /workspace ] && [ -w /workspace ]; then
            cache_parent=/workspace/.cache
        fi
        export HF_HOME="${cache_parent}/huggingface"
        if [ "${old_cache}" != "${HF_HOME}" ] && [ -d "${old_cache}" ] && [ ! -e "${HF_HOME}" ]; then
            echo "==> Moving existing model cache to ${HF_HOME} (preserving downloaded files)..."
            mkdir -p -- "${cache_parent}"
            mv -- "${old_cache}" "${HF_HOME}"
            # Keep older tools pointing at the same files rather than a second copy.
            ln -s -- "${HF_HOME}" "${old_cache}"
        elif [ "${old_cache}" != "${HF_HOME}" ] && [ -d "${old_cache}" ] && [ ! -L "${old_cache}" ]; then
            # Never silently discard an existing cache if both locations exist.
            echo "==> Both model cache locations exist; reusing ${old_cache}. Set HF_HOME to choose another location."
            export HF_HOME="${old_cache}"
        fi
    fi
    export HF_HUB_CACHE="${HF_HUB_CACHE:-${HUGGINGFACE_HUB_CACHE:-${TRANSFORMERS_CACHE:-${HF_HOME}/hub}}}"
    export HF_XET_CACHE="${HF_XET_CACHE:-${HF_HOME}/xet}"
    mkdir -p -- "${HF_HUB_CACHE}" "${HF_XET_CACHE}"
    echo "==> Model cache: ${HF_HUB_CACHE} — reusing completed files; downloading only missing files"
}

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

# كل إقلاع يبدأ من الصفر: نحذف بيئة بايثون وكل كاش الموديلات، ثم يعيد
# السكربت تنزيلهم بالإصدارات المثبَّتة (requirements*.txt). هذا يمنع بقايا
# تنزيل ناقص أو حزم من تشغيلة سابقة من التأثير على التشغيل، ويحرر مساحة
# الـ Volume قبل التنزيل. FRESH_START=0 bash start.sh يتخطى الحذف.
_reset_environment() {
    local path
    for path in .venv-pipecat "${HF_HOME:-}" /workspace/.cache "${PWD}/.cache" \
        "${HOME}/.cache/huggingface" "${HOME}/.cache/vllm" "${HOME}/.cache/pip"; do
        if [ -n "${path}" ] && { [ -e "${path}" ] || [ -L "${path}" ]; }; then
            echo "==> Removing ${path}"
            rm -rf -- "${path}"
        fi
    done
}
if [ "${FRESH_START:-1}" = "1" ]; then
    _boot_stage "Removing the Python environment and all model caches (fresh start)"
    _reset_environment
fi

# Pipecat 1.12 requires Python >=3.11. Never alter /usr/bin/python3.
_boot_stage "Selecting the Python runtime"
if [ -f .venv-pipecat/bin/activate ]; then
    source .venv-pipecat/bin/activate
fi
if [ ! -f .venv-pipecat/bin/activate ] || ! python3 -c 'import sys; assert (3,11) <= sys.version_info[:2] < (3,14)' 2>/dev/null; then
    echo "==> Preparing isolated Python 3.12.12 for Pipecat..."
    python3 -m pip install --no-cache-dir -q uv==0.9.5
    python3 -m uv python install 3.12.12
    python3 -m uv venv --python 3.12.12 --seed .venv-pipecat
    source .venv-pipecat/bin/activate
fi

_boot_stage "Removing cached pip downloads (installed packages and models are preserved)"
python3 -m pip cache purge >/dev/null 2>&1 || true
_boot_stage "Preparing persistent model storage"
_prepare_model_cache
_ensure_python_dependencies
_boot_stage "Checking audio system libraries"
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

# 8001 مستخدَم أحياناً من خدمة نظام على قوالب RunPod العامة (لاحظنا nginx
# داخلي ماسكه بالفعل على بعض الـ Pods) — 18001 منفذ داخلي (بين الحاويتين
# فقط، ما يحتاج Expose من لوحة RunPod) أقل عرضة للتصادم. غيّره من
# app/config.py لو لسا يتصادم بمنفذك.
# الإعدادات ثابتة في app/config.py. نقرأها بعد تثبيت متطلبات بايثون كي يكون
# الاسم والمنفذ وحدود vLLM متطابقة دائماً بين FastAPI وسكربت التشغيل.
# GPU_MEMORY_UTILIZATION بالبيئة (مثل GPU_MEMORY_UTILIZATION=0.80 bash start.sh)
# يتجاوز 0.85: config.py يقرأه، فالقيمة اللي تطلع هنا هي نفسها اللي يشوفها FastAPI.
_boot_stage "Reading model and port configuration"
# A process substitution hides Python's exit status from set -e/mapfile.
STATIC_CONFIG_TEXT=$(python3 -c '
from app.config import settings
for value in (
    settings.model_name, settings.vllm_port, settings.api_port,
    settings.max_model_len, settings.gpu_memory_utilization,
    settings.max_num_seqs, settings.max_num_batched_tokens,
    int(settings.kv_cache_memory_gb * 1024**3),
):
    print(value)
')
mapfile -t STATIC_CONFIG <<< "${STATIC_CONFIG_TEXT}"
MODEL_NAME="${STATIC_CONFIG[0]}"
VLLM_PORT="${STATIC_CONFIG[1]}"
API_PORT="${STATIC_CONFIG[2]}"
MAX_MODEL_LEN="${STATIC_CONFIG[3]}"
GPU_MEMORY_UTILIZATION="${STATIC_CONFIG[4]}"
MAX_NUM_SEQS="${STATIC_CONFIG[5]}"
MAX_NUM_BATCHED_TOKENS="${STATIC_CONFIG[6]}"
KV_CACHE_BYTES="${STATIC_CONFIG[7]}"
VLLM_LOG="/tmp/vllm_boot.log"
VLLM_ARGS_TEXT=$(python3 -m app.core.vllm_launch)
mapfile -t VLLM_ARGS <<< "${VLLM_ARGS_TEXT}"


# يقابل CPU Thread Pool Size في LM Studio.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-8}"

# الشرح: تنزيل الموديلات بدون أي توكن Hugging Face. كل المستودعات اللي
# نستعملها عامة وغير gated، فما نحتاج مصادقة إطلاقاً. لكن huggingface_hub
# يدوّر على توكن تلقائياً (متغير HF_TOKEN، أو الاسم القديم
# HUGGING_FACE_HUB_TOKEN، أو ملف ~/.cache/huggingface/token من login سابق)
# ويرسله مع كل طلب لو لقاه. الخطر: توكن قديم/ملغى ضابطه قالب RunPod يخلّي
# الـHub يرد 401 حتى على مستودع عام. لذلك:
#   · unset يمسح متغيرَي التوكن من بيئة هذا السكربت — وبالتالي من vLLM
#     وuvicorn (native audio/F5-TTS) لأنهم يرثون البيئة منه.
#   · HF_HUB_DISABLE_IMPLICIT_TOKEN=1 يمنع إرسال توكن ملف login المخزَّن
#     ضمنياً مع طلبات القراءة/التنزيل.
# unset HF_TOKEN HUGGING_FACE_HUB_TOKEN
# export HF_HUB_DISABLE_IMPLICIT_TOKEN=1

# حزمة pip nvidia-cuda-nvcc-cu12 (المُثبَّتة تبعاً لـvllm) لا توفّر nvcc
# فعلياً بهذا الإصدار — فيها ptxas وlibnvvm.so بس، بدون الملف التنفيذي nvcc
# نفسه. flashinfer (يستخدمه vLLM افتراضياً لتسريع sampling) يحتاج nvcc
# لتجميع كيرنلاته وقت التشغيل (JIT، أول استدعاء توليد فعلي)، فينهار كامل
# محرك vLLM لاحقاً جداً (بعد تحميل الأوزان) بخطأ "Could not find nvcc".
# تركيب CUDA toolkit كامل حل ثقيل غير ضروري: عندنا مسار بديل مبني بـPyTorch
# العادي لا يحتاج JIT إطلاقاً — نعطّل flashinfer sampler صراحة بمتغير البيئة
# الرسمي لـvLLM (VLLM_USE_FLASHINFER_SAMPLER=0) بدل الاعتماد على افتراضه
# التلقائي (يحاول flashinfer إذا "موجود" حتى لو تجميعه سيفشل عملياً).
export VLLM_USE_FLASHINFER_SAMPLER=0

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
    nvidia_pkg_dir=$(python3 -c "import nvidia, os; print(os.path.dirname(nvidia.__path__[0]))" 2>/dev/null) || nvidia_pkg_dir=""
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

_fix_cuda_nvcc_path() {
    # flashinfer (يستخدمه vLLM لعمليات sampling) يجمّع كيرنلات CUDA وقت
    # التشغيل الفعلي (JIT، أول استدعاء حقيقي — يحدث بعد تحميل الأوزان بكثير،
    # فلا يظهر إلا متأخراً جداً بمرحلة تشغيل المحرك الفعلي) ويحتاج nvcc قابل
    # للتنفيذ + CUDA_HOME صحيح. عندنا nvcc مثبَّت كحزمة pip
    # (nvidia-cuda-nvcc-cu12) لا كتثبيت نظام تقليدي بـ/usr/local/cuda، فـ
    # flashinfer يفشل بـ"Could not find nvcc and default cuda_home=... doesn't
    # exist" رغم أن nvcc موجود فعلاً بعمق site-packages. نفس فكرة
    # _fix_cuda_lib_path أعلاه، بس لملف تنفيذي (nvcc) بدل مكتبة مشتركة.
    local nvcc_path cuda_home
    nvcc_path=$(python3 -c "
import importlib.util, os
spec = importlib.util.find_spec('nvidia.cuda_nvcc')
if spec and spec.submodule_search_locations:
    print(spec.submodule_search_locations[0])
" 2>/dev/null) || nvcc_path=""
    if [ -z "${nvcc_path}" ]; then
        echo "==> nvidia-cuda-nvcc-cu12 pip package not found — skipping nvcc path fix"
        return
    fi
    nvcc_path=$(find "${nvcc_path}" -maxdepth 2 -type f -name nvcc 2>/dev/null | head -1)
    if [ -z "${nvcc_path}" ]; then
        echo "==> No nvcc executable found under the nvidia-cuda-nvcc-cu12 package — this fix doesn't apply here"
        return
    fi
    cuda_home=$(dirname "$(dirname "${nvcc_path}")")
    export CUDA_HOME="${cuda_home}"
    export PATH="${cuda_home}/bin:${PATH}"
    echo "==> Found nvcc at ${nvcc_path} — set CUDA_HOME=${cuda_home}"
}

_start_vllm() {
    # يخلّف العملية بالخلفية بالشل الحالي نفسه (لا command substitution:
    # subshell $(...) يفقد ملكية العملية الخلفية بمجرد خروجه ببعض بيئات
    # bash، فيوصل PID لعملية ميتة بالشل الأم). النتيجة تُقرأ مباشرة من $!
    # بعد الاستدعاء عبر المتغير العالمي VLLM_LAST_PID.
    python3 -m vllm.entrypoints.openai.api_server "${VLLM_ARGS[@]}" > "${VLLM_LOG}" 2>&1 &
    VLLM_LAST_PID=$!
}


# نطبّق إصلاحي مسار CUDA بشكل استباقي (لا رجعي فقط) قبل أول محاولة تشغيل:
# فشل flashinfer بالذات (nvcc) قد يحدث متأخراً جداً — بعد تحميل الأوزان
# بالكامل وبعد ما start.sh يكون تجاوز نافذة فحص "مات فوراً" أدناه ويعتبر
# vLLM شغّالاً — فما يُكتشف تلقائياً لو انتظرنا رد فعل بعد الانهيار فقط.
# كل من الدالتين آمنة تماماً لو ما احتاجتها البيئة (تتحقق وتخرج بصمت).
_boot_stage "Checking optional CUDA library and compiler paths"
_fix_cuda_lib_path
_fix_cuda_nvcc_path
_boot_stage "Verifying vLLM imports with CUDA paths configured"
python3 -c "import vllm.entrypoints.openai.api_server"

_boot_stage "Starting vLLM"
# Keep this shell alive as supervisor. exec uvicorn would discard the EXIT
# trap and leave the vLLM process behind when the API stops.
trap _cleanup_services EXIT
trap 'exit 143' TERM
trap 'exit 130' INT
echo "voice_memory_plan kv_cache_bytes=${KV_CACHE_BYTES} auto_gpu_fraction=${GPU_MEMORY_UTILIZATION} max_sequences=${MAX_NUM_SEQS} tts_instances=${TTS_POOL_SIZE:-1}"
echo "==> Starting vLLM on port ${VLLM_PORT} (model: ${MODEL_NAME}, gpu_memory_utilization=${GPU_MEMORY_UTILIZATION})..."
_start_vllm
VLLM_PID=${VLLM_LAST_PID}

# ننتظر شوي ونتأكد أن العملية ما ماتت فوراً (فشل استيراد/تحميل مبكر) قبل ما
# نكمل — أخطاء لاحقة (OOM أثناء تحميل الأوزان) تبقى تظهر باللوق لاحقاً.
sleep 8
if ! kill -0 "${VLLM_PID}" 2>/dev/null; then
    echo "🛑 vLLM died immediately — boot log:"
    tail -n 40 "${VLLM_LOG}"
    if grep -qi "libcudart\|libcublas\|cannot open shared object file" "${VLLM_LOG}"; then
        echo "==> Cause: CUDA runtime library path is missing — fixing LD_LIBRARY_PATH..."
        _fix_cuda_lib_path
    else
        echo "🛑 vLLM startup failed — check the log above. Installed packages will not be reinstalled automatically."
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

# Pipecat runs inside FastAPI over /voice_followup/stream; no separate worker.

echo "==> Starting FastAPI on port ${API_PORT}..."
echo "    (features run immediately in fallback mode; the readiness check in app/core/engine.py"
echo "     switches them to model mode automatically once vLLM finishes loading weights)"
_boot_stage "Starting FastAPI (loading application imports)"
python3 -u -m uvicorn app.main:app --host 0.0.0.0 --port "${API_PORT}" --workers 1 &
API_PID=$!
_supervise_services
