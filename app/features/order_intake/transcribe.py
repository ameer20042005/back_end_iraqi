# -*- coding: utf-8 -*-
"""تحويل صوت لنص عبر Whisper — عربي (ayoubkirouane/whisper-small-ar افتراضياً)
أو كردي سوراني (roshna-omer/whisper-small-Kurdish-Sorani). موديلات
transformers عادية (وليست CTranslate2)، لذا نستخدم pipeline قياسي بدل
faster-whisper.

**السرعة**: الموديل يُحمَّل على الـ GPU إن توفّر (كان يشتغل على الـ CPU دائماً
لأن pipeline بلا `device` يختار CPU افتراضياً — أبطأ بمرّات على ملف صوتي
حقيقي)، وبنصف الدقة على الـ GPU. والملفات الأطول من 30 ثانية تُقطَّع تلقائياً
(`chunk_length_s`) لأن Whisper يقرأ أول 30 ثانية فقط بدونها فيضيع باقي الطلب.

**اللغة**: بالعربي نُثبّت "arabic" + مهمة النسخ صراحةً — بدونها Whisper يكتشف
اللغة تلقائياً وقد يترجم الكلام العراقي للإنجليزية أحياناً بدل نسخه عربياً.
بالكردي **ما نمرر رمز لغة**: الموديل مفرَّغ على لغة خارج قائمة Whisper الـ99
(ماكو ku ولا ckb بيها)، وفرض رمز لغة ثانية عليه يخرّب مخرجه.

⚠️ **اللغة تجي من المستدعي، لا من الملف الصوتي.** ما تكدر تعرف لغة الصوت
قبل ما تحوّله لنص — وموديلنا العربي مفرَّغ على العربية وحدها، فخاصية اكتشاف
اللغة بـWhisper الأصلي ما عادت موثوقة بيه: يرجّع عربي دائماً حتى لو الكلام
كردي صرف (حروف عربية بلا معنى، لا خطأ واضح تكدر تكشفه). البديل — تشغيل
الموديلين وأخذ الأفضل — مرفوض عمداً: يضاعف زمن الاستجابة واستهلاك الـVRAM
بكل طلب، مقابل تخمين ما يزال غير مضمون.

التحميل بطيء أول مرة (تنزيل الأوزان)؛ يصير مرة واحدة ويُخزَّن بالكاش.
"""

import logging
from time import monotonic
from typing import Optional

try:
    from transformers import pipeline

    _TRANSFORMERS_AVAILABLE = True
except ImportError:
    _TRANSFORMERS_AVAILABLE = False

from app.config import settings
from app.lang import Lang

logger = logging.getLogger(__name__)

# قاموس بدل متغير مفرد: نحتاج موديلين محمّلين **بنفس الوقت** أحياناً (زبون
# عربي وزبون كردي بنفس اللحظة)، ومتغير واحد يعني تفريغ وإعادة تحميل بكل
# تبديل لغة — عشرات الثواني بكل مكالمة. المفتاح رمز اللغة، والقيمة
# (pipeline, آخر وقت استعمال) حتى نعرف أي موديل صار خامل ونفرّغه.
_pipelines: "dict[str, tuple]" = {}

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


def _lang_config(lang: Lang) -> tuple:
    """يرجع (اسم الموديل، معطيات التوليد) من اختيار config المركزي."""
    model = settings.stt_model_for(lang)
    gen = {"task": "transcribe"}
    if model.language:
        gen["language"] = model.language
    return model.repository, gen


def _evict_idle(keep: str) -> None:
    """يفرّغ الموديلات الخاملة قبل تحميل موديل جديد.

    هذي **مو رفاهية**: تعليقات tts.py توثّق سقوطاً حقيقياً بالإنتاج سببه
    نفاد ذاكرة الـGPU لأن vLLM يحجز أغلب VRAM على A40. الموديل العربي
    مستثنى دائماً لأنه المسار الأكثر استعمالاً، وتفريغه يعاقب الأغلبية
    لأجل الأقلية."""
    ttl = settings.ku_model_idle_unload_seconds
    if ttl <= 0:
        return
    now = monotonic()
    for key in [k for k in _pipelines if k not in (keep, Lang.AR.value)]:
        _pipe, last_used = _pipelines[key]
        if now - last_used < ttl:
            continue
        del _pipelines[key]
        logger.info("تفريغ موديل الصوت %s بعد خمول %d ثانية", key, ttl)
        try:
            import gc

            import torch

            gc.collect()
            torch.cuda.empty_cache()
        except Exception:
            pass


def _get_pipeline(lang: Optional[Lang] = None):
    """يحمّل (أو يرجّع المحمَّل مسبقاً) خط الاستدلال للغة المطلوبة."""
    lang = lang or Lang.AR
    key = lang.value
    if key in _pipelines:
        pipe, _last = _pipelines[key]
        _pipelines[key] = (pipe, monotonic())
        return pipe

    _evict_idle(keep=key)
    model_name, _gen = _lang_config(lang)

    device, torch_dtype = -1, None
    try:
        import torch

        if torch.cuda.is_available():
            device, torch_dtype = 0, torch.float16
    except ImportError:
        pass

    logger.info(
        "تحميل موديل تحويل الصوت %s (%s) على %s (أول مرة قد تستغرق دقائق للتنزيل)...",
        model_name, key, "GPU" if device == 0 else "CPU",
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
    _pipelines[key] = (pipe, monotonic())
    return pipe


def warmup() -> bool:
    """يحمّل الموديل **العربي** (ويشغّله على صمت قصير) عند إقلاع الخادم بدل
    أول طلب حقيقي.

    بدون هذا، أول رسالة صوتية يدفع صاحبها ثمن تحميل الأوزان **ونسخ نواة
    CUDA الأولى** — عشرات الثواني تظهر للمستخدم كأنها بطء بالتحويل نفسه،
    بينما الطلبات اللاحقة أسرع بمرّات. يرجع True إن جهز الموديل فعلاً.

    ⚠️ الموديل الكردي **ما ينحمّل هنا عمداً** (تحميل كسول): تحميل موديلين
    إضافيين عند الإقلاع يحجز VRAM دائماً ويخاطر بـOOM يسقط المسار العربي
    الأكثر استعمالاً. ينحمّل أول ما يوصل زبون كردي فعلاً."""
    if not _TRANSFORMERS_AVAILABLE:
        return False
    try:
        import numpy as np

        pipe = _get_pipeline(Lang.AR)
        _model, gen_kwargs = _lang_config(Lang.AR)
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


def transcribe(audio_bytes: bytes, lang: Optional[Lang] = None) -> Optional[str]:
    """يحوّل بايتات ملف صوتي (wav/mp3/m4a/ogg...) لنص باللغة المطلوبة.

    يرجع None إذا transformers غير مثبَّتة (محلياً بدون GPU) — المستدعي
    يقرر كيف يتعامل مع الحالة هذي — وسلسلة فارغة إذا ما كان بالملف كلام
    مفهوم.

    `lang` افتراضه العربي، فأي مستدعٍ قديم يبقى يشتغل حرفياً بلا تعديل —
    وهذا مقصود: التوسعة للكردية ما تفرض تعديل كل نقطة نداء بالمشروع دفعة
    وحدة. انظر أعلى الملف ليش ما ينكشف من الصوت نفسه."""
    if not _TRANSFORMERS_AVAILABLE:
        return None
    lang = lang or Lang.AR
    _model, gen_kwargs = _lang_config(lang)
    result = _get_pipeline(lang)(audio_bytes, generate_kwargs=gen_kwargs)
    return (result.get("text") or "").strip()
