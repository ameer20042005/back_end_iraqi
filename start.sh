#!/bin/bash
# سكربت الإقلاع على RunPod Pod
# استخدمه إذا رفعت الكود مباشرة على Pod شغال بقالب runpod/pytorch
# (بدون بناء Docker image مخصصة):
#   cd /workspace/back_end_iraqi && bash start.sh

set -e

cd "$(dirname "$0")"

echo "==> تثبيت المتطلبات..."
pip install --no-cache-dir -r requirements.txt -r requirements-gpu.txt

echo "==> تشغيل FastAPI على المنفذ 8000..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
