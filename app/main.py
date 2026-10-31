"""FastAPI application, engine lifecycle, and feature router registration."""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.setdefault("LOGURU_LEVEL", "WARNING")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.core.engine import (
    AI_MODEL_NOT_LOADED,
    AI_REQUEST_TIMEOUT,
    LM_STUDIO_UNAVAILABLE,
    LLMUpstreamError,
    llm_engine,
)
from app.core.model_status import get_model_status
from app.features.district_correction.router import load_catalog as load_district_catalog
from app.features.district_correction.router import router as district_correction_router
from app.features.order_intake.router import router as order_intake_router
from app.features.openai_compat.router import router as openai_compat_router
from app.features.sales.router import router as sales_router
from app.features.voice_followup import voice_health
from app.features.voice_followup.router import router as voice_followup_router
from app.core.voice_logging import configure_diagnostics
from app.core.system_backend import close_client as close_system_backend_client
from app.core.catalog import close_client as close_catalog_client

configure_diagnostics()

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


async def _warmup_audio_models() -> None:
    """Load audio models after vLLM has reserved GPU memory."""
    while not llm_engine.ready:
        await asyncio.sleep(5)
    # موديلات مكالمات WebSocket (VOICE_STREAM_WARMUP، افتراضياً على GPU بس).
    if voice_health.warmup_enabled():
        await run_in_threadpool(voice_health.warmup_voice_models)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await llm_engine.start()
    load_district_catalog(app)
    warmup_task = asyncio.create_task(_warmup_audio_models())
    try:
        yield
    finally:
        warmup_task.cancel()
        await asyncio.gather(warmup_task, return_exceptions=True)
        await llm_engine.shutdown()
        await close_system_backend_client()
        await close_catalog_client()


app = FastAPI(
    title="Iraqi Backend API",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# حالة HTTP لكل رمز خطأ: 503/504 مؤقتة (يجوز للعميل إعادة المحاولة).
_UPSTREAM_STATUS_BY_CODE = {
    LM_STUDIO_UNAVAILABLE: 503,
    AI_MODEL_NOT_LOADED: 503,
    AI_REQUEST_TIMEOUT: 504,
}


@app.exception_handler(LLMUpstreamError)
async def llm_upstream_error_handler(request: Request, exc: LLMUpstreamError):
    """يحوّل أخطاء خادم النموذج إلى رد برمز ثابت بدل 500 عارية.

    ``error.code`` هو العقد الآلي مع jbot؛ ``detail`` يبقى للقراءة البشرية."""
    return JSONResponse(
        status_code=_UPSTREAM_STATUS_BY_CODE.get(exc.code, 502),
        content={
            "error": {"code": exc.code, "message": exc.message, "type": "upstream_error"},
            "detail": f"خادم النموذج رفض الطلب: {exc.message}",
            "upstream_status": exc.upstream_status,
            "model": settings.model_name,
        },
    )


app.include_router(sales_router)
app.include_router(openai_compat_router)
app.include_router(district_correction_router)
app.include_router(order_intake_router)
app.include_router(voice_followup_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "back_end_iraqi", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/health/llm")
async def llm_health():
    """هل خادم النموذج متاح والموديل محمَّل؟ — فحص قبل الاختبارات."""
    status = await get_model_status(force=True)
    healthy = status.reachable and status.loaded is not False
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={"status": "healthy" if healthy else "unhealthy", **status.as_dict()},
    )


@app.get("/health/voice")
def voice_health_check():
    """موديلات صوت المكالمات (F5-TTS + Voxtral): مثبَّتة؟ محمَّلة؟ الذاكرة
    الحرة، ونتيجة آخر محاولة نطق. ما يحمّل أي موديل. 503 لو مكتبة ناقصة أو آخر
    نطق فشل أو فشل التحميل المسبق."""
    status = voice_health.voice_status()
    return JSONResponse(status_code=200 if status["status"] == "healthy" else 503, content=status)


@app.get("/metrics")
def metrics():
    """إحصاءات عميل vLLM (app/core/engine.py) — عدد الطلبات، الأخطاء، أزمنة
    استجابة p50/p95، وجاهزية خادم vLLM. لإحصاءات المحرك الداخلية التفصيلية
    (KV cache، طلبات نشطة...) انظر /metrics على منفذ خادم vLLM نفسه (8001)."""
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
        "vllm_ready": llm_engine.ready,
    }
    if torch.cuda.is_available():
        info["device_count"] = torch.cuda.device_count()
        info["device_name"] = torch.cuda.get_device_name(0)
        info["cuda_version"] = torch.version.cuda
        free, total = torch.cuda.mem_get_info(0)
        info["vram_total_gb"] = round(total / 1024**3, 2)
        info["vram_free_gb"] = round(free / 1024**3, 2)
    return info
