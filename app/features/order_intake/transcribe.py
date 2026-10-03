# -*- coding: utf-8 -*-
"""نسخ الصوت العربي عبر Whisper مع تحميل واحد وتقطيع التسجيلات الطويلة.

تُثبّت لغة التوليد العربية ومهمة النسخ لتفادي ترجمة الكلام العراقي.
يُستخدم GPU بنصف الدقة إن توفّر، وتُعالَج المقاطع بدفعات مع تراكب.
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

_pipeline = None

# Whisper يعالج 30 ثانية بالمرة — بدون تقطيع يُقصّ أي ملف أطول بصمت.
# الرسائل الصوتية بالواتساب توصل لدقائق، فالتقطيع ضروري لا تحسين.
_CHUNK_LENGTH_S = 30
# تراكب بين القطع حتى لا تنقطع كلمة على الحدّ فتضيع. transformers يقبل
# (يسار، يمين) — تمريره كرقم واحد يجعل التراكب 1/6 من القطعة على الجهتين
# (5 ثوانٍ لكل جهة)، أي إعادة معالجة 10 ثوانٍ زائدة بكل قطعة. الزوج الصريح
# يقلّل الهدر للنصف مع بقاء الحماية من قطع الكلمة على الحدّ.
_CHUNK_OVERLAP_S = (3, 2)
# القطع مستقلة عن بعضها، فالـ GPU يعالجها **دفعة واحدة** بدل واحدة-واحدة.
# هذا أكبر مكسب سرعة بمسار الصوت: رسالة دقيقتين = 4 قطع كانت تتسلسل، صارت
# استدلالاً واحداً. 16 آمنة على GPU بذاكرة أكبر من A40 (مثل A100 40GB فما فوق)
# بموديل small بنصف الدقة — راقب استهلاك الذاكرة إذا صار out-of-memory.
_GPU_BATCH_SIZE = 16

# على الـ CPU الدفعات ما تنفع (ماكو توازي حقيقي) وتزيد استهلاك الذاكرة فقط.
_CPU_BATCH_SIZE = 1


def _model_config() -> tuple:
    """يرجع (اسم الموديل، معطيات التوليد) من اختيار config المركزي."""
    model = settings.stt_model()
    gen = {"task": "transcribe"}
    if model.language:
        gen["language"] = model.language
    return model.repository, gen


def _get_pipeline():
    """تحميل نسخة واحدة من موديل النسخ العربي وإعادة استخدامها."""
    global _pipeline
    if _pipeline is not None:
        return _pipeline
    model_name, _gen = _model_config()

    device, torch_dtype = -1, None
    try:
        import torch

        if torch.cuda.is_available():
            device, torch_dtype = 0, torch.float16
    except ImportError:
        pass

    logger.info(
        "تحميل موديل تحويل الصوت %s على %s (أول مرة قد تستغرق دقائق للتنزيل)...",
        model_name, "GPU" if device == 0 else "CPU",
    )
    kwargs = {
        "model": model_name,
        "device": device,
        "chunk_length_s": _CHUNK_LENGTH_S,
        "stride_length_s": _CHUNK_OVERLAP_S,
        "batch_size": _GPU_BATCH_SIZE if device == 0 else _CPU_BATCH_SIZE,
    }
    if torch_dtype is not None:
        kwargs["torch_dtype"] = torch_dtype
    pipe = pipeline("automatic-speech-recognition", **kwargs)
    _pipeline = pipe
    return pipe


def warmup() -> bool:
    """يحمّل الموديل **العربي** (ويشغّله على صمت قصير) عند إقلاع الخادم بدل
    أول طلب حقيقي.

    بدون هذا، أول رسالة صوتية يدفع صاحبها ثمن تحميل الأوزان **ونسخ نواة
    CUDA الأولى** — عشرات الثواني تظهر للمستخدم كأنها بطء بالتحويل نفسه،
    بينما الطلبات اللاحقة أسرع بمرّات. يرجع True إن جهز الموديل فعلاً.
    """
    if not _TRANSFORMERS_AVAILABLE:
        return False
    try:
        import numpy as np

        pipe = _get_pipeline()
        _model, gen_kwargs = _model_config()
        # ثانية صمت بـ 16kHz (معدل Whisper) — تكفي لتنفيذ مسار الاستدلال كاملاً
        # وتجهيز النواة، بلا تحميل ملف من القرص.
        pipe(
            {"raw": np.zeros(16000, dtype="float32"), "sampling_rate": 16000},
            generate_kwargs=gen_kwargs,
        )
        logger.info("✅ موديل تحويل الصوت جاهز (تحميل مسبق مكتمل)")
        return True
    except Exception:
        # الإحماء تحسين لا شرط تشغيل — الفشل هنا ما يمنع إقلاع الخادم،
        # والتحميل الكسول بأول طلب يبقى المسار البديل.
        logger.warning("تعذّر التحميل المسبق لموديل الصوت — سيُحمَّل عند أول طلب", exc_info=True)
        return False


def transcribe(audio_bytes: bytes) -> Optional[str]:
    """نسخ ملف صوتي للعربية؛ None عند غياب المكتبة، ونص فارغ عند الصمت."""
    if not _TRANSFORMERS_AVAILABLE:
        return None
    _model, gen_kwargs = _model_config()
    result = _get_pipeline()(audio_bytes, generate_kwargs=gen_kwargs)
    return (result.get("text") or "").strip()
