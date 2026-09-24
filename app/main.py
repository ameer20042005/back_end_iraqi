"""FastAPI application, engine lifecycle, and feature router registration."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.engine import llm_engine
from app.features.order_intake.router import router as order_intake_router
from app.features.openai_compat.router import router as openai_compat_router
from app.features.order_intake.transcribe import warmup as warmup_transcriber
from app.features.sales.router import router as sales_router
from app.features.voice_followup.router import router as voice_followup_router
from app.features.voice_followup.tts import warmup as warmup_tts
from app.system_backend import close_client as close_system_backend_client

try:
    import torch
    TORCH_AVAILABLE = True
except ImportError:
    TORCH_AVAILABLE = False


async def _warmup_audio_models() -> None:
    """Load audio models after vLLM has reserved GPU memory."""
    while not llm_engine.ready:
        await asyncio.sleep(5)
    await run_in_threadpool(warmup_transcriber)
    await run_in_threadpool(warmup_tts)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await llm_engine.start()
    warmup_task = asyncio.create_task(_warmup_audio_models())
    try:
        yield
    finally:
        warmup_task.cancel()
        await llm_engine.shutdown()
        await close_system_backend_client()


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
    # بدون هذا، JS بالمتصفح ما يقدر يقرأ هيدرات الرد المخصَّصة حتى لو نجح
    # الطلب فعلياً (قيد CORS قياسي: فقط هيدرات "safelisted" مقروءة افتراضياً).
    # مسار المتابعة الصوتية (app/features/voice_followup/router.py) يرجع
    # session_id ونتيجة التحليل بهذي الهيدرات بدل جسم JSON (الجسم صوت خام) —
    # static/index.html يقرأها مباشرة لعرض التجربة الصوتية الكاملة.
    # X-Reply-Text/X-Call-Status/X-Chosen-Option/X-Postpone-Saved تخص مكالمة
    # تأجيل التسليم (شخصية صباح، /postpone/start و/postpone/respond).
    expose_headers=[
        "X-Session-Id", "X-Question-Text",
        "X-Reason-Summary", "X-Customer-Transcript", "X-Query-Sent",
        "X-Reply-Text", "X-Call-Status", "X-Chosen-Option", "X-Postpone-Saved",
    ],
)

app.include_router(sales_router)
app.include_router(openai_compat_router)
app.include_router(order_intake_router)
app.include_router(voice_followup_router)


@app.get("/")
def root():
    return {"status": "ok", "service": "back_end_iraqi", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/metrics")
def metrics():
    """إحصاءات عميل vLLM (app/engine.py) — عدد الطلبات، الأخطاء، أزمنة
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
