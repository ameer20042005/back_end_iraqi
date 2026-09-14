# -*- coding: utf-8 -*-
"""تحويل نص لصوت — عراقي (ameer4wisam/Habibi-TTS-IRQ) أو كردي سوراني
(aranemini/central-kurdish-tts، من مبادرة TTS4All). كلاهما **نفس معمارية
F5-TTS**، فنفس صنف F5TTS ونفس دالة infer() يخدمان الاثنين — يتبدّل بس
ثلاثي (نقطة التفتيش، المفردات، الصوت المرجعي).

خلافاً لـ Whisper (app/features/order_intake/transcribe.py)، هذا **ليس**
موديل transformers.pipeline قياسي — هو موديل zero-shot voice cloning يحتاج
ثلاثة مدخلات لكل توليد:

    ref_audio  — مقطع صوتي مرجعي بصوت المتكلم المطلوب تقليده.
    ref_text   — النص الحرفي المنطوق بـ ref_audio (ثابت، مطابق للتسجيل).
    gen_text   — النص المطلوب توليد صوت له (سؤال المتابعة أو الشكر).

**اللغة تُكتشف تلقائياً من النص** (app/lang.py::detect) خلافاً لـ
transcribe.py اللي تحتاج المستدعي يخبرها. السبب أن الاتجاه معكوس: هناك
ندخل صوتاً ونطلع نصاً فاللغة مجهولة لين ننتهي، وهنا ندخل **نصاً** كتبناه
إحنا (رد صباح) فاللغة معروفة قبل ما نبدأ.

⚠️ ما ينفع ننطق الكردي بالموديل العربي: vocab.txt العربي مبني من نص عراقي،
وحروف السوراني المميزة (ە ۆ ێ ڕ ڵ ڤ ژ گ چ پ) مو موجودة بيه — تنسقط بصمت
وتطلع كلمات مبتورة، لا رسالة خطأ تنبّهك.

نقاط التفتيش تُنزَّل مرة واحدة عبر huggingface_hub وتُخزَّن بالكاش المحلي
القياسي لـ HF (~/.cache/huggingface).

يُستخدم حصراً بمسار المتابعة الصوتية (app/features/voice_followup/router.py).
"""

import logging
from pathlib import Path
from time import monotonic
from typing import Optional

try:
    from f5_tts.api import F5TTS

    _F5_TTS_AVAILABLE = True
except ImportError:
    _F5_TTS_AVAILABLE = False

from app.config import settings
from app.lang import Lang, detect

logger = logging.getLogger(__name__)

# قاموس النسخ المحمَّلة، مفتاحه رمز اللغة، وقيمته
# (النسخة، الصوت المرجعي، نصّه، آخر وقت استعمال).
#
# ليش الثلاثي مخزون مع النسخة بدل متغيرات عامة منفصلة؟ لأن فصلها يفتح باب
# خلط مرجع لغة بموديل لغة ثانية — وهو خطأ ما يرمي استثناءً، يطلع صوتاً
# رديئاً بس، فما تكتشفه إلا بالسماع.
_instances: "dict[str, tuple]" = {}


def _download_hub_assets(model) -> tuple:
    """ينزّل ملفات نقطة تفتيش TTS التي يكون مرجعها محفوظاً في HF Hub.

    الصوت المرجعي ونصّه ينزّلان من المستودع نفسه (خلافاً للعربي اللي مرجعه
    ملف محلي reference/IRQ.wav) لأن المستودع الكردي يرفق زوجاً مضبوطاً لكل
    صوت، وتسجيل مرجع كردي بأنفسنا يحتاج متحدثاً أصلياً وجودة استوديو.

    ⚠️ النص المرجعي يُقرأ من الملف، ما يُكتب يدوياً: F5-TTS يقارن النص
    بالتسجيل حرفياً، وأي فرق حرف واحد يخرّب جودة الاستنساخ بصمت."""
    from huggingface_hub import hf_hub_download

    repo = model.repository
    ckpt = hf_hub_download(repo_id=repo, filename=model.checkpoint)
    vocab = hf_hub_download(repo_id=repo, filename=model.vocabulary)
    ref_audio = hf_hub_download(repo_id=repo, filename=model.reference_audio)
    ref_text_file = hf_hub_download(repo_id=repo, filename=model.reference_text)
    ref_text = Path(ref_text_file).read_text(encoding="utf-8").strip()
    return ckpt, vocab, ref_audio, ref_text


def _evict_idle(keep: str) -> None:
    """يفرّغ موديل النطق الخامل قبل تحميل غيره — نفس مبرر _evict_idle
    بـtranscribe.py (ضغط VRAM مع vLLM على A40، موثّق بالسقوط الحقيقي
    المشروح بـsynthesize أدناه). العربي مستثنى دائماً لأنه الأكثر
    استعمالاً."""
    ttl = settings.ku_model_idle_unload_seconds
    if ttl <= 0:
        return
    now = monotonic()
    for key in [k for k in _instances if k not in (keep, Lang.AR.value)]:
        if now - _instances[key][3] < ttl:
            continue
        del _instances[key]
        logger.info("تفريغ موديل النطق %s بعد خمول %d ثانية", key, ttl)
        try:
            import gc

            import torch

            gc.collect()
            torch.cuda.empty_cache()
        except Exception:
            pass


def _get_instance(lang: Optional[Lang] = None) -> tuple:
    """يحمّل (أو يرجّع المحمَّل مسبقاً) نسخة الموديل للغة المطلوبة مع صوتها
    المرجعي ونصّه. يرجع ثلاثياً لأن دالة التوليد تحتاج الثلاثة معاً."""
    lang = lang or Lang.AR
    key = lang.value
    if key in _instances:
        inst, ref_audio, ref_text, _last = _instances[key]
        _instances[key] = (inst, ref_audio, ref_text, monotonic())
        return inst, ref_audio, ref_text

    _evict_idle(keep=key)

    model = settings.tts_model_for(lang)
    if model.reference_assets_on_hub:
        logger.info(
            "تحميل موديل النطق %s (%s، أول مرة قد تستغرق دقائق للتنزيل)...",
            model.repository, key,
        )
        ckpt_file, vocab_file, ref_audio, ref_text = _download_hub_assets(model)
    else:
        from huggingface_hub import hf_hub_download

        logger.info(
            "تحميل موديل النطق %s (%s، أول مرة قد تستغرق دقائق للتنزيل)...",
            model.repository, key,
        )
        ckpt_file = hf_hub_download(repo_id=model.repository, filename=model.checkpoint)
        vocab_file = hf_hub_download(repo_id=model.repository, filename=model.vocabulary)
        ref_audio = str(Path(__file__).resolve().parent / model.reference_audio)
        ref_text = model.reference_text

    inst = F5TTS(ckpt_file=ckpt_file, vocab_file=vocab_file)

    # إصلاح تعارض دقة (dtype) داخل مكتبة f5_tts نفسها (1.1.22): الموديل
    # الرئيسي (self.ema_model) يُرقّى تلقائياً لـfloat16 على أي GPU بقدرة
    # حوسبة ≥6 (utils_infer.py::load_checkpoint، شرط torch.cuda
    # .get_device_properties(device).major >= 6 — يشمل A40/A100/H100)،
    # ومخرَج التوليد (mel spectrogram) يُحوَّل صراحةً لfloat32 قبل الـ
    # vocoder (utils_infer.py:519) — لكن self.vocoder (Vocos) نفسه يبقى
    # كما حُمِّل بدون أي تحويل دقة صريح بـload_vocoder، فيطلع أحياناً
    # float16 هو الآخر (حسب كيف يحمّله Vocos.from_pretrained داخلياً)،
    # فيتعارض مع مدخل float32 المضمون صراحةً: RuntimeError: mat1 and
    # mat2 must have the same dtype, but got Float and Half. لا خيار
    # dtype مكشوف بواجهة F5TTS العامة (__init__) لضبط هذا من الخارج،
    # فنفرضه هنا صراحة بعد التحميل مباشرة على self.vocoder وحده — الموديل
    # الرئيسي يبقى float16 كما صمَّمته المكتبة (أسرع، ومطابق لما يتوقعه
    # بقية الأنبوب). ينطبق على الموديلين لأنه خلل بالمكتبة لا بنقطة تفتيش.
    if hasattr(inst, "vocoder"):
        inst.vocoder = inst.vocoder.float()

    _instances[key] = (inst, ref_audio, ref_text, monotonic())
    return inst, ref_audio, ref_text


def warmup() -> bool:
    """يحمّل موديل النطق **العربي** (أوزان + مرجع الصوت) عند إقلاع الخادم
    بدل أول طلب حقيقي — نفس مبرر transcribe.warmup (انظر هناك). يرجع True
    إن جهز الموديل فعلاً.

    الموديل الكردي ما ينحمّل هنا عمداً (تحميل كسول) — انظر transcribe.warmup."""
    if not _F5_TTS_AVAILABLE:
        return False
    try:
        synthesize("مرحبا", lang=Lang.AR)
        logger.info("✅ موديل تحويل النص لصوت جاهز (تحميل مسبق مكتمل)")
        return True
    except Exception:
        logger.warning("تعذّر التحميل المسبق لموديل TTS — سيُحمَّل عند أول طلب", exc_info=True)
        return False


def synthesize(text: str, lang: Optional[Lang] = None) -> Optional[bytes]:
    """يحوّل نصاً (عراقي أو كردي سوراني) لملف صوتي WAV (بايتات) بصوت مناسب
    للغته. يرجع None إذا مكتبة f5_tts غير مثبَّتة (محلياً بدون GPU) أو فشل
    التوليد فعلياً (تعارض إصدار numpy، نفاد ذاكرة GPU...) — المستدعي
    (router) يقرر كيف يتعامل مع الحالة (503 واضح للزبون بدل انهيار 500 خام
    بلا رسالة).

    `lang=None` تعني "اكتشفها من النص" وهو السلوك المطلوب بأغلب النداءات،
    لأن النص هو رد صباح اللي ولّده النموذج بلغة الزبون أصلاً. تمريرها
    صراحةً يبقى متاحاً لمن يريد يفرضها (الإحماء، أو رد احتياطي ثابت).

    ⚠️ استثناءات infer() تُلتقط هنا صراحةً وتُسجَّل بالتفصيل (traceback كامل)
    بدل ما تتسرّب كـ500 غير مفسَّر لـFastAPI — لقطة إنتاج حقيقية: /ask كان
    يرجع 500 بلا أي تفصيل، والسبب الفعلي (numpy 2.x مثبَّتة عبر تبعيات vLLM
    بينما f5-tts يتطلب <=1.26.4، أو OOM لأن vLLM يحجز أغلب VRAM على A40)
    ما كان يظهر بأي مكان — هذا يخلي السبب الحقيقي يظهر بـ/tmp/api.log فوراً."""
    if not _F5_TTS_AVAILABLE:
        return None
    if not text.strip():
        return None

    import io

    import soundfile as sf

    lang = lang or detect(text)
    try:
        inst, ref_audio, ref_text = _get_instance(lang)
        wav, sr, _spec = inst.infer(
            ref_file=str(ref_audio),
            ref_text=ref_text,
            gen_text=text,
            # speed: 1.0 هو الافتراضي (طبيعي) — 0.85 يبطّئ الإلقاء شوي بلا ما
            # يوصل لدرجة غير طبيعية، بطلب صريح لصوت أوضح وأهدأ بمكالمة متابعة.
            speed=0.85,
            # target_rms: مستوى تطبيع الصوت (RMS) — الافتراضي 0.1 خافت نسبياً
            # بمكالمة هاتفية. رفعه لـ0.18 يرفع الصوت ملموساً بلا قص (clipping)
            # واضح، لأنه تطبيع بمستوى الطاقة لا مضاعفة خام للعينات.
            target_rms=0.18,
            # nfe_step: عدد خطوات التوليد (diffusion) — الافتراضي 32. رفعه
            # لـ48 يحسّن وضوح النطق ملموساً مقابل وقت توليد أطول قليلاً، مقبول
            # هنا لأن الميزة غير حساسة لزمن استجابة لحظي (مكالمة متابعة، لا
            # محادثة نصية مباشرة).
            nfe_step=48,
        )
        buf = io.BytesIO()
        sf.write(buf, wav, sr, format="WAV")
        return buf.getvalue()
    except Exception:
        logger.exception("فشل توليد الصوت (%s) — النص: %r", lang, text[:100])
        return None
