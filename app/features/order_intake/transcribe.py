# -*- coding: utf-8 -*-
"""تحويل صوت لنص عربي عبر موديل Whisper مفرَّغ على العربية (ayoubkirouane/whisper-small-ar
افتراضياً — قابل للتغيير بـ WHISPER_MODEL). موديل transformers عادي (وليس
CTranslate2)، لذا نستخدم pipeline قياسي بدل faster-whisper.

**السرعة**: الموديل يُحمَّل على الـ GPU إن توفّر (كان يشتغل على الـ CPU دائماً
لأن pipeline بلا `device` يختار CPU افتراضياً — أبطأ بمرّات على ملف صوتي
حقيقي)، وبنصف الدقة على الـ GPU. والملفات الأطول من 30 ثانية تُقطَّع تلقائياً
(`chunk_length_s`) لأن Whisper يقرأ أول 30 ثانية فقط بدونها فيضيع باقي الطلب.

**اللغة**: نُثبّت العربية + مهمة النسخ صراحةً — بدونها Whisper يكتشف اللغة
تلقائياً وقد يترجم الكلام العراقي للإنجليزية أحياناً بدل نسخه عربياً.

التحميل بطيء أول مرة (تنزيل الأوزان)؛ يصير مرة واحدة ويُخزَّن بالكاش.
"""

import logging
from typing import Optional

try:
    from transformers import pipeline

    _TRANSFORMERS_AVAILABLE = True
except ImportError:
    _TRANSFORMERS_AVAILABLE = False

from app.config import settings

logger = logging.getLogger(__name__)

_asr_pipeline = None

# Whisper يعالج 30 ثانية بالمرة — بدون تقطيع يُقصّ أي ملف أطول بصمت.
# الرسائل الصوتية بالواتساب توصل لدقائق، فالتقطيع ضروري لا تحسين.
_CHUNK_LENGTH_S = 30
# تراكب بين القطع حتى لا تنقطع كلمة على الحدّ فتضيع.
_CHUNK_OVERLAP_S = 5


def _get_pipeline():
    global _asr_pipeline
    if _asr_pipeline is None:
        device, torch_dtype = -1, None
        try:
            import torch

            if torch.cuda.is_available():
                device, torch_dtype = 0, torch.float16
        except ImportError:
            pass

        logger.info(
            "تحميل موديل تحويل الصوت %s على %s (أول مرة قد تستغرق دقائق للتنزيل)...",
            settings.whisper_model, "GPU" if device == 0 else "CPU",
        )
        kwargs = {
            "model": settings.whisper_model,
            "device": device,
            "chunk_length_s": _CHUNK_LENGTH_S,
            "stride_length_s": _CHUNK_OVERLAP_S,
        }
        if torch_dtype is not None:
            kwargs["torch_dtype"] = torch_dtype
        _asr_pipeline = pipeline("automatic-speech-recognition", **kwargs)
    return _asr_pipeline


def transcribe(audio_bytes: bytes) -> Optional[str]:
    """يحوّل بايتات ملف صوتي (wav/mp3/m4a/ogg...) لنص عربي. يرجع None إذا
    transformers غير مثبَّتة (محلياً بدون GPU) — المستدعي يقرر كيف يتعامل مع
    الحالة هذي — وسلسلة فارغة إذا ما كان بالملف كلام مفهوم."""
    if not _TRANSFORMERS_AVAILABLE:
        return None
    result = _get_pipeline()(
        audio_bytes,
        # نسخ عربي صراحةً بدل الاكتشاف التلقائي (اللي يترجم للإنجليزية أحياناً).
        generate_kwargs={"language": "arabic", "task": "transcribe"},
    )
    return (result.get("text") or "").strip()
