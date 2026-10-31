"""One lazily loaded Arabic Voxtral shared by calls and uploaded voice notes.

Loading and inference are serialized off the event loop. No raw audio reaches
the dialogue model, and health inspection never downloads or loads weights.
"""

import asyncio
import importlib.util
import logging
import re
import threading
import time

import numpy as np

from app.config import settings
from app.features.voice_followup import vad

logger = logging.getLogger(__name__)
MAX_AUDIO_SECONDS = 30
_lock = threading.Lock()
_model = None
_processor = None
_device = None
_last_transcription = None

# عبارات تهلوسها نماذج النسخ على الصمت أو الضوضاء (من ترجمات الفيديو التي تتدرّب عليها).
_HALLUCINATIONS = re.compile(
    r"ترجمة\s*نانسي\s*قنقر|ترجمة\s*نانسي|اشتركوا?\s*(في|ب)\s*القناة|لا\s*تنسوا?\s*الاشتراك"
    r"|شكرا\s*(لكم\s*)?(على\s*ال|لل)مشاهدة|\[موسيقى\]|subscribe|amara\.org", re.IGNORECASE)
# كلمة أو عبارة (حتى 3 كلمات) تتكرر 3 مرات فأكثر ورا بعض = حلقة تكرار من النموذج.
_REPEAT =re.compile(r"(?<!\S)(\S+(?:\s+\S+){0,2}?)(?:\s+\1){2,}(?!\S)")
# أقل من هذا (RMS) يعتبر صمتاً لا يستحق النسخ.
_MIN_RMS = 0.003


def clean_transcript(text: str) -> str:
    """يحذف الهلوسات المعروفة ويختصر حلقات التكرار."""
    if _HALLUCINATIONS.search(text):
        text = _HALLUCINATIONS.sub(" ", text)
    text = _REPEAT.sub(r"\1", text)
    text = re.sub(r"\s+", " ", text).strip(" .،,")
    return text


def _load():
    global _model, _processor, _device
    if _model is not None:
        return
    from app.features.voice_followup.voice_health import ModelLoadError, NotInstalledError

    try:
        import mistral_common  # noqa: F401  (the Voxtral processor's tokenizer backend)
        import torch
        from transformers import AutoProcessor, VoxtralRealtimeForConditionalGeneration
    except ImportError as exc:
        raise NotInstalledError("ثبّت torch وtransformers وmistral-common[audio] لتشغيل Voxtral") from exc
    try:
        device = settings.stt_device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        processor = AutoProcessor.from_pretrained(settings.stt_model)
        # BF16 is the published precision, on CPU too: float32 would double
        # the 8.9GB of weights.
        model = VoxtralRealtimeForConditionalGeneration.from_pretrained(
            settings.stt_model, dtype=torch.bfloat16,
        ).to(device).eval()
        _model, _processor, _device = model, processor, device
    except Exception as exc:
        raise ModelLoadError("تعذّر تحميل موديل النسخ الصوتي العربي") from exc


def transcribe_array(samples: np.ndarray, sample_rate: int = vad.SAMPLE_RATE) -> str:
    global _last_transcription
    started = time.monotonic()
    samples = np.asarray(samples, dtype=np.float32)
    if (sample_rate != vad.SAMPLE_RATE or samples.ndim != 1
            or not 0 < samples.size <= MAX_AUDIO_SECONDS * sample_rate
            or not np.isfinite(samples).all()):
        raise ValueError("Voxtral requires 0–30 seconds of finite mono 16kHz audio")
    if float(np.sqrt(np.mean(samples ** 2))) < _MIN_RMS:
        return ""
    with _lock:
        try:
            _load()
            import torch

            inputs = _processor(samples, return_tensors="pt").to(_device, dtype=_model.dtype)
            with torch.inference_mode():
                tokens = _model.generate(**inputs, do_sample=False)
            text = clean_transcript(_processor.batch_decode(tokens, skip_special_tokens=True)[0])
            _last_transcription = {"ok": True, "error_type": None,
                                   "elapsed_ms": int((time.monotonic() - started) * 1000)}
            return text
        except Exception as exc:
            _last_transcription = {"ok": False, "error_type": type(exc).__name__,
                                   "elapsed_ms": int((time.monotonic() - started) * 1000)}
            raise


async def transcribe(samples: np.ndarray, sample_rate: int = vad.SAMPLE_RATE) -> str:
    async with asyncio.timeout(settings.stt_timeout_seconds):
        return await asyncio.to_thread(transcribe_array, samples, sample_rate)


def warmup() -> bool:
    # يحمّل الموديل مباشرة: النسخ يتخطى الصمت قبل _load() فلا يحمّل شيئاً.
    try:
        with _lock:
            _load()
        return True
    except Exception:
        logger.exception("فشل تجهيز Voxtral")
        return False


def status() -> dict:
    return {"engine": "voxtral", "model": settings.stt_model, "local_model": True,
            "installed": all(importlib.util.find_spec(name) is not None
                             for name in ("torch", "transformers", "mistral_common")),
            "loaded": _model is not None, "device": _device,
            "last_transcription": dict(_last_transcription) if _last_transcription else None}
