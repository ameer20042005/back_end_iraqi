# -*- coding: utf-8 -*-
"""نسخ كلام الزبون العربي بالمكالمة الحية عبر Whisper — نسخة خاصة بهذه الميزة.

مستقلة عمداً عن app/features/order_intake/transcribe.py (كل ميزة مستقلة بحد
ذاتها)، وتعيش بعملية الـ agent worker. المدخل مقطع كلام واحد قصير (يقطعه VAD
عند سكوت الزبون) كمصفوفة PCM، فلا حاجة لفك ملف ولا لتقطيع 30 ثانية.
"""

import logging
import threading
from typing import Optional

try:
    from transformers import pipeline

    _TRANSFORMERS_AVAILABLE = True
except ImportError:
    _TRANSFORMERS_AVAILABLE = False

from app.config import settings

logger = logging.getLogger(__name__)

_pipeline = None
_lock = threading.Lock()


def _generate_kwargs() -> dict:
    model = settings.stt_model()
    gen = {"task": "transcribe"}
    if model.language:
        gen["language"] = model.language
    return gen


def _get_pipeline():
    """تحميل نسخة واحدة من موديل النسخ العربي وإعادة استخدامها."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline

    device, torch_dtype = -1, None
    try:
        import torch

        if torch.cuda.is_available():
            device, torch_dtype = 0, torch.float16
    except ImportError:
        pass

    model_name = settings.stt_model().repository
    logger.info("تحميل موديل النسخ %s على %s...", model_name, "GPU" if device == 0 else "CPU")
    kwargs = {"model": model_name, "device": device}
    if torch_dtype is not None:
        kwargs["torch_dtype"] = torch_dtype
    _pipeline = pipeline("automatic-speech-recognition", **kwargs)
    return _pipeline


def available() -> bool:
    return _TRANSFORMERS_AVAILABLE


def transcribe_pcm(samples, sample_rate: int) -> Optional[str]:
    """ينسخ مصفوفة float32 أحادية القناة. None عند غياب المكتبة.

    القفل يمنع مكالمتين متزامنتين من استدعاء نفس الـ pipeline سوية (الـ worker
    يشغّل المكالمات كخيوط بنفس العملية حتى تُحمَّل الأوزان مرة وحدة)."""
    if not _TRANSFORMERS_AVAILABLE:
        return None
    with _lock:
        result = _get_pipeline()(
            {"raw": samples, "sampling_rate": sample_rate},
            generate_kwargs=_generate_kwargs(),
        )
    return (result.get("text") or "").strip()


def warmup() -> bool:
    """يحمّل الموديل ويشغّله على ثانية صمت قبل أول مكالمة."""
    if not _TRANSFORMERS_AVAILABLE:
        return False
    try:
        import numpy as np

        transcribe_pcm(np.zeros(16000, dtype="float32"), 16000)
        logger.info("✅ موديل النسخ جاهز للمكالمات")
        return True
    except Exception:
        logger.warning("تعذّر التحميل المسبق لموديل النسخ — سيُحمَّل بأول مكالمة", exc_info=True)
        return False
