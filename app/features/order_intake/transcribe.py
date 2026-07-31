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
            "batch_size": _GPU_BATCH_SIZE if device == 0 else _CPU_BATCH_SIZE,
        }
        if torch_dtype is not None:
            kwargs["torch_dtype"] = torch_dtype
        _asr_pipeline = pipeline("automatic-speech-recognition", **kwargs)
    return _asr_pipeline


def warmup() -> bool:
    """يحمّل الموديل (ويشغّله على صمت قصير) عند إقلاع الخادم بدل أول طلب حقيقي.

    بدون هذا، أول رسالة صوتية يدفع صاحبها ثمن تحميل الأوزان **ونسخ نواة CUDA
    الأولى** — عشرات الثواني تظهر للمستخدم كأنها بطء بالتحويل نفسه، بينما
    الطلبات اللاحقة أسرع بمرّات. يرجع True إن جهز الموديل فعلاً."""
    if not _TRANSFORMERS_AVAILABLE:
        return False
    try:
        import numpy as np

        pipe = _get_pipeline()
        # ثانية صمت بـ 16kHz (معدل Whisper) — تكفي لتنفيذ مسار الاستدلال كاملاً
        # وتجهيز النواة، بلا تحميل ملف من القرص.
        pipe(
            {"raw": np.zeros(16000, dtype="float32"), "sampling_rate": 16000},
            generate_kwargs={"language": "arabic", "task": "transcribe"},
        )
        logger.info("✅ موديل تحويل الصوت جاهز (تحميل مسبق مكتمل)")
        return True
    except Exception:
        # الإحماء تحسين لا شرط تشغيل — الفشل هنا ما يمنع إقلاع الخادم،
        # والتحميل الكسول بأول طلب يبقى المسار البديل.
        logger.warning("تعذّر التحميل المسبق لموديل الصوت — سيُحمَّل عند أول طلب", exc_info=True)
        return False


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
