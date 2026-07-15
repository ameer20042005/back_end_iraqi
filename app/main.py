"""FastAPI backend — يعمل محلياً وعلى RunPod (قالب PyTorch) مع transformers + RAG.

كل ميزة براوترها الخاص تحت app/features/*/router.py — هذا الملف فقط ينشئ
التطبيق، يشغّل دورة حياة المحرك (lifespan)، ويجمع كل الراوترات."""

from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app.engine import llm_engine
from app.features.order_intake.router import router as order_intake_router
from app.features.sales.router import router as sales_router
from app.features.support.router import router as support_router

try:
    import torch

    TORCH_AVAILABLE = True
except ImportError:
    # محلياً بدون torch — على RunPod تكون المكتبة موجودة في الصورة
    TORCH_AVAILABLE = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # يحمّل موديل transformers مرة واحدة عند الإقلاع، ويشغّل worker الـ
    # micro-batching بالخلفية (انظر app/engine.py لتفاصيل الاختيار عن vLLM).
    # محلياً بدون GPU/transformers يبقى llm_engine.ready == False وكل الميزات
    # ترجع لوضع fallback (بدون توليد نموذج) حتى يشتغل الكود فعلياً على RunPod.
    await llm_engine.start()
    yield
    # يلغي worker الـ batching ويفشل أي طلبات لسا بالطابور بدل تركها معلَّقة.
    await llm_engine.shutdown()


app = FastAPI(
    title="Iraqi Backend API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(sales_router)
app.include_router(support_router)
app.include_router(order_intake_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "back_end_iraqi", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/metrics")
def metrics():
    """إحصاءات worker الـ micro-batching (app/engine.py) — عدد الطلبات،
    متوسط حجم الدفعة، أزمنة استجابة p50/p95، عمق الطابور الحالي."""
    return llm_engine.get_metrics()


_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/test", include_in_schema=False)
def test_console():
    """لوحة اختبار API تفاعلية (HTML/CSS/JS ثابتة، بدون تبعيات) — انظر static/index.html."""
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/gpu")
def gpu_info():
    """معلومات الـ GPU — للتأكد أن CUDA شغالة على RunPod."""
    if not TORCH_AVAILABLE:
        return {"torch": None, "cuda": False, "note": "torch غير مثبت محلياً"}
    info = {
        "torch": torch.__version__,
        "cuda": torch.cuda.is_available(),
        "model_ready": llm_engine.ready,
    }
    if torch.cuda.is_available():
        info["device_count"] = torch.cuda.device_count()
        info["device_name"] = torch.cuda.get_device_name(0)
        info["cuda_version"] = torch.version.cuda
        free, total = torch.cuda.mem_get_info(0)
        info["vram_total_gb"] = round(total / 1024**3, 2)
        info["vram_free_gb"] = round(free / 1024**3, 2)
    return info
