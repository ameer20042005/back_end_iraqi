# قالب RunPod: PyTorch 2.8.0 + CUDA 12.8.1 + Ubuntu 24.04
FROM runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404

WORKDIR /workspace/app

# ffmpeg لازم لتحويل الصوت لنص (app/features/order_intake/transcribe.py)
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-gpu.txt .
RUN pip install --no-cache-dir -r requirements.txt -r requirements-gpu.txt

COPY . .

EXPOSE 8000

# شغّل الـ API مباشرة. إذا تريد الاحتفاظ بخدمات RunPod (SSH/Jupyter)
# استخدم start.sh بدلاً من هذا السطر: CMD ["/workspace/app/start.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
