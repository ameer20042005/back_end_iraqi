# -*- coding: utf-8 -*-
"""صحة موديلات الصوت (Whisper + Chatterbox) بعملية uvicorn: تصنيف أسباب الفشل،
ذاكرة الـ GPU، حالة التحميل المسبق، وملخّص GET /health/voice.

**ما يحمّل أي موديل:** `voice_status()` تقرأ الحالة بس. التحميل الوحيد هنا
بـ `warmup_voice_models()`، ويشتغل بس لما `VOICE_STREAM_WARMUP` مفعّل.
"""

import logging
import sys
import threading
import time
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)

# أسباب الفشل اللي تنرسل للتطبيق بحقل reason (بلا تفاصيل حساسة).
CUDA_OOM = "cuda_oom"
NOT_INSTALLED = "not_installed"
LOAD_FAILED = "load_failed"
GENERATION_FAILED = "generation_failed"


class NotInstalledError(RuntimeError):
    """المكتبة (chatterbox-tts / transformers) غير مثبَّتة أو استيرادها فشل."""


class ModelLoadError(RuntimeError):
    """فشل تحميل أوزان الموديل أو بصمة الصوت (السبب الأصلي بـ __cause__)."""


def _chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _is_oom(exc: BaseException) -> bool:
    torch = sys.modules.get("torch")
    oom_type = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
    if isinstance(oom_type, type) and isinstance(exc, oom_type):
        return True
    text = str(exc).lower()
    return "out of memory" in text and ("cuda" in text or "cublas" in text or "gpu" in text)


def classify_error(exc: Optional[BaseException]) -> str:
    """يحوّل استثناء النطق لسبب مختصر: نفاد الذاكرة يغلب أي غلاف فوقه."""
    if exc is None:
        return GENERATION_FAILED
    chain = list(_chain(exc))
    if any(_is_oom(e) for e in chain):
        return CUDA_OOM
    if any(isinstance(e, (NotInstalledError, ImportError)) for e in chain):
        return NOT_INSTALLED
    if any(isinstance(e, ModelLoadError) for e in chain):
        return LOAD_FAILED
    return GENERATION_FAILED


def gpu_memory() -> dict:
    """الذاكرة الحرة/الكلية على الـ GPU بالـ GB. فارغ لو ماكو CUDA."""
    try:
        import torch

        if not torch.cuda.is_available():
            return {"cuda": False}
        free, total = torch.cuda.mem_get_info(0)
        allocated, reserved = torch.cuda.memory_allocated(0), torch.cuda.memory_reserved(0)
        return {"cuda": True, "vram_free_gb": round(free / 1024**3, 2),
                "vram_total_gb": round(total / 1024**3, 2),
                "process_allocated_gb": round(allocated / 1024**3, 2),
                "process_reserved_gb": round(reserved / 1024**3, 2),
                "process_cached_unused_gb": round((reserved - allocated) / 1024**3, 2)}
    except Exception:
        return {"cuda": False}


def gpu_free_gb() -> Optional[float]:
    return gpu_memory().get("vram_free_gb")


def describe_failure(exc: Optional[BaseException]) -> str:
    """سطر لوق واحد: السبب، نوع الاستثناء ورسالته، والذاكرة الحرة لحظتها."""
    reason = classify_error(exc)
    detail = f"{type(exc).__name__}: {exc}" if exc is not None else "لا صوت (None)"
    free = gpu_free_gb()
    return f"reason={reason} error={detail} vram_free_gb={free if free is not None else 'n/a'}"


# ── التحميل المسبق ─────────────────────────────────────────────────────────

def _cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def warmup_enabled() -> bool:
    """VOICE_STREAM_WARMUP=1/0 صريح، وإلا مفعّل افتراضياً على GPU بس."""
    if settings.voice_stream_warmup is not None:
        return settings.voice_stream_warmup
    return _cuda_available()


_warmup_lock = threading.Lock()
_warmup = {"state": "pending", "tts": None, "stt": None, "seconds": None}


def warmup_voice_models() -> dict:
    """يجهّز Whisper وChatterbox قبل أول مكالمة، ويسجّل الفشل بوضوح."""
    from app.features.voice_followup import tts, stt

    with _warmup_lock:
        _warmup.update(state="running", tts=None, stt=None, seconds=None)
        started = time.monotonic()
        mode = "whisper"
        stt_ok = stt.warmup()
        tts_ok = tts.warmup()
        _warmup.update(state="done" if tts_ok and stt_ok else "failed", tts=tts_ok, stt=stt_ok,
                       seconds=round(time.monotonic() - started, 1))
        if tts_ok and stt_ok:
            logger.info("✅ موديلات صوت المكالمات جاهزة (%.1f ثانية)", _warmup["seconds"])
        else:
            logger.error("🛑 فشل التحميل المسبق لموديلات صوت المكالمات: chatterbox=%s whisper=%s %s — "
                         "التفاصيل بـ GET /health/voice", tts_ok, stt_ok, gpu_memory())
        return {**_warmup, "input_mode": mode}


def warmup_state() -> dict:
    state = dict(_warmup)
    if not warmup_enabled() and state["state"] == "pending":
        state["state"] = "disabled"
    return {"enabled": warmup_enabled(), **state}


_last_call: Optional[dict] = None


def record_last_call(snapshot: dict) -> None:
    """إحصاءات صوت آخر مكالمة WebSocket (أرقام بس) — تنعرض بـ last_call."""
    global _last_call
    _last_call = dict(snapshot)


def last_call() -> Optional[dict]:
    return dict(_last_call) if _last_call else None


def voice_status() -> dict:
    """ملخّص GET /health/voice — يقرأ الحالة بس، ما يحمّل موديل."""
    from app.features.voice_followup import tts, stt

    tts_status, warm = tts.status(), warmup_state()
    stt_status = stt.status()
    mode = "whisper"
    last = tts_status.get("last_synthesis") or {}
    from app.core.engine import llm_engine

    stt_status["ready"] = (stt_status["installed"] and (stt_status["loaded"] or not warm["enabled"])
                           and (stt_status.get("last_transcription") or {}).get("ok") is not False)
    healthy = (tts_status["installed"] and llm_engine.ready and stt_status["ready"]
               and last.get("ok") is not False and warm["state"] != "failed")
    return {
        "status": "healthy" if healthy else "unhealthy",
        "tts": tts_status,
        "stt": stt_status,
        "llm": {"model": settings.model_name, "ready": llm_engine.ready},
        "gpu": gpu_memory(),
        "warmup": warm,
        "last_call": last_call(),
        "engine": "pipecat",
        "input_mode": mode,
        "stt_required": True,
        "memory_plan": {"kv_cache_gb": settings.kv_cache_memory_gb,
                        "gpu_fraction": settings.gpu_memory_utilization,
                        "max_sequences": settings.max_num_seqs, "tts_pool_size": settings.tts_pool_size},
    }
