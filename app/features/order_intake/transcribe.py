# -*- coding: utf-8 -*-
"""تحويل صوت لنص عربي عبر موديل Whisper مفرَّغ على العربية (ayoubkirouane/whisper-small-ar
افتراضياً — قابل للتغيير بـ WHISPER_MODEL). موديل transformers عادي (وليس
CTranslate2)، لذا نستخدم pipeline قياسي بدل faster-whisper.
"""

from typing import Optional

try:
    from transformers import pipeline

    _TRANSFORMERS_AVAILABLE = True
except ImportError:
    _TRANSFORMERS_AVAILABLE = False

from app.config import settings

_asr_pipeline = None


def _get_pipeline():
    global _asr_pipeline
    if _asr_pipeline is None:
        _asr_pipeline = pipeline("automatic-speech-recognition", model=settings.whisper_model)
    return _asr_pipeline


def transcribe(audio_bytes: bytes) -> Optional[str]:
    """يحوّل بايتات ملف صوتي (wav/mp3/m4a...) لنص. يرجع None إذا transformers
    غير مثبَّتة (محلياً بدون GPU) — المستدعي يقرر كيف يتعامل مع الحالة هذي."""
    if not _TRANSFORMERS_AVAILABLE:
        return None
    result = _get_pipeline()(audio_bytes)
    return (result.get("text") or "").strip()
