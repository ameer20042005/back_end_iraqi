#!/bin/bash
# سكربت التشغيل المحلي: يشغّل FastAPI فقط (منفذ api_port من app/config.py،
# افتراضياً 8000) ويوجّهه لخادم LLM محلي متوافق مع OpenAI شغّال أصلاً على
# http://localhost:1234/v1 (المنفذ الافتراضي لـ LM Studio) — بدل خادم vLLM
# اللي يثبّته ويشغّله start.sh على RunPod.
#
# الفرق عن start.sh:
#   · ما يثبّت أي شي بالنظام (لا apt ولا CUDA ولا vLLM) — مصمَّم لجهازك
#     المحلي (Windows عبر Git Bash، أو Linux/macOS) وفيه .venv جاهز.
#   · ما يشغّل خادم الموديل بنفسه — خادم الـLLM المحلي تشغّله أنت قبله
#     وتحمّل فيه الموديل.
#   · يمرر الرابط واسم الموديل للتطبيق عبر متغيرَي البيئة LLM_BASE_URL
#     وLLM_MODEL (يقرأهما app/config.py).
#
# الاستخدام:
#   bash start_local.sh
#   LLM_MODEL=<model-id> bash start_local.sh                     # موديل محدد
#   LLM_BASE_URL=http://localhost:11434/v1 bash start_local.sh   # خادم آخر

set -e
cd "$(dirname "$0")"

# ── البيئة الافتراضية (venv) ──────────────────────────────────────────────
# نفعّل .venv بجذر المشروع حتى يشتغل python وuvicorn من حزمه لا من بايثون
# النظام. على Windows (Git Bash) ملف التفعيل تحت .venv/Scripts، وعلى
# Linux/macOS تحت .venv/bin. python أولاً لأن python3 على Windows غالباً مجرد
# اختصار يفتح متجر مايكروسوفت.
if [ -f .venv/Scripts/activate ]; then
    source .venv/Scripts/activate
elif [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
else
    echo "⚠️ No .venv found — using the system Python"
fi
PYTHON=$(command -v python || command -v python3)

# ── متطلبات بايثون الأساسية ───────────────────────────────────────────────
# محلياً يكفي requirements.txt لأن التوليد كله بخادم الـLLM المحلي.
# requirements-gpu.txt (torch/Whisper/F5-TTS) ثقيلة ومخصصة لـ RunPod؛ ثبّتها
# يدوياً فقط لو تريد تجرّب ميزات الصوت محلياً. وبعكس start.sh نثبّت فقط لو
# الحزم ناقصة فعلاً — جهازك يحتفظ بها بين التشغيلات.
"${PYTHON}" -c "import fastapi, uvicorn, httpx" 2>/dev/null || {
    echo "==> Installing base requirements (requirements.txt)..."
    "${PYTHON}" -m pip install -q -r requirements.txt
}

# ── رابط خادم الـLLM المحلي ───────────────────────────────────────────────
# export ضروري حتى ترثه عملية uvicorn فيقرأه app/config.py. ميزة تصحيح
# المناطق تستعمله أيضاً ما دام DISTRICT_LLM_BASE_URL فارغاً.
export LLM_BASE_URL="${LLM_BASE_URL:-http://localhost:1234/v1}"
# الموديلات المحلية أبطأ بكثير من vLLM (دقيقة أو أكثر للطلب الواحد مع التفكير)، فمهلة
# تصحيح المناطق الافتراضية (15 ثانية) تنتهي قبل الجواب — نرفعها للتشغيل المحلي فقط.
export DISTRICT_LLM_TIMEOUT_SECONDS="${DISTRICT_LLM_TIMEOUT_SECONDS:-300}"

# ── فحص الخادم واكتشاف اسم الموديل ───────────────────────────────────────
# app/engine.py يرسل اسم الموديل بحقل "model" بكل طلب، والخادم المحلي يتوقع
# معرّف الموديل المحمَّل فيه فعلاً لا اسم مستودع Hugging Face الثابت. فلو ما
# حددت LLM_MODEL نسأل GET /models وناخذ أول موديل محادثة — نتخطى موديلات
# الـembedding لأن LM Studio يعرضها بنفس القائمة. urllib (مكتبة قياسية) بدل
# curl + jq حتى ما نعتمد على أدوات ممكن ما تكون مثبَّتة على Windows.
# لو الخادم ما رد ما نوقف الإقلاع: engine.py يعيد الفحص كل 10 ثوانٍ.
if [ -z "${LLM_MODEL}" ]; then
    LLM_MODEL=$("${PYTHON}" -c '
import json, os, urllib.request
url = os.environ["LLM_BASE_URL"].rstrip("/") + "/models"
try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        ids = [m["id"] for m in json.load(resp)["data"]]
    print(next(i for i in ids if "embed" not in i.lower()))
except Exception:
    pass
')
fi
if [ -n "${LLM_MODEL}" ]; then
    export LLM_MODEL
    echo "==> Local LLM: ${LLM_BASE_URL} (model: ${LLM_MODEL})"
else
    echo "⚠️ Could not reach ${LLM_BASE_URL}/models, or no model is loaded there."
    echo "   Start your local LLM server and load a model, or set it explicitly:"
    echo "   LLM_MODEL=<model-id> bash start_local.sh"
fi

# ── تشغيل FastAPI ─────────────────────────────────────────────────────────
# المنفذ من app/config.py (نفس أسلوب start.sh) حتى يبقى مصدر الإعدادات واحداً.
# "python -m uvicorn" يضمن uvicorn المثبَّت بالـ venv، و127.0.0.1 بدل 0.0.0.0
# لأن التشغيل محلي للتجربة. exec يخلّي Ctrl+C يوصل uvicorn مباشرة.
API_PORT=$("${PYTHON}" -c 'from app.config import settings; print(settings.api_port)')
echo "==> Starting FastAPI on http://127.0.0.1:${API_PORT}"
echo "    test console: http://127.0.0.1:${API_PORT}/test  ·  docs: http://127.0.0.1:${API_PORT}/docs"
exec "${PYTHON}" -m uvicorn app.main:app --host 127.0.0.1 --port "${API_PORT}" --workers 1
