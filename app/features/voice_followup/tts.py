# -*- coding: utf-8 -*-
"""توليد صوت المتابعة العراقي بموديل F5-TTS ومرجع صوت محلي ثابت.

تُنزل الأوزان والمفردات من Hugging Face وتُعاد الاستفادة من نسخة واحدة.
الصوت المرجعي ونصّه الحرفي محددان في إعدادات التطبيق.
"""

import logging
from pathlib import Path
from typing import Optional

try:
    from f5_tts.api import F5TTS

    _F5_TTS_AVAILABLE = True
except ImportError:
    _F5_TTS_AVAILABLE = False

from app.config import settings

logger = logging.getLogger(__name__)

_instance = None


def _get_instance() -> tuple:
    """تحميل نسخة النطق العربي مع مرجعها مرة واحدة."""
    global _instance
    if _instance is not None:
        return _instance

    from huggingface_hub import hf_hub_download

    model = settings.tts_model()
    logger.info("تحميل موديل النطق %s (أول مرة قد تستغرق دقائق للتنزيل)...", model.repository)
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
    # بقية الأنبوب). هذا الإصلاح يخص دقة مخرجات المكتبة.
    if hasattr(inst, "vocoder"):
        inst.vocoder = inst.vocoder.float()

    _instance = (inst, ref_audio, ref_text)
    return _instance


def warmup() -> bool:
    """يحمّل موديل النطق **العربي** (أوزان + مرجع الصوت) عند إقلاع الخادم
    بدل أول طلب حقيقي — نفس مبرر transcribe.warmup (انظر هناك). يرجع True
    إن جهز الموديل فعلاً."""
    if not _F5_TTS_AVAILABLE:
        return False
    try:
        if synthesize("مرحبا") is None:
            return False
        logger.info("✅ موديل تحويل النص لصوت جاهز (تحميل مسبق مكتمل)")
        return True
    except Exception:
        logger.warning("تعذّر التحميل المسبق لموديل TTS — سيُحمَّل عند أول طلب", exc_info=True)
        return False


def synthesize(text: str) -> Optional[bytes]:
    """يحوّل النص لملف صوتي WAV (بايتات) بصوت عراقي. يرجع None إذا مكتبة f5_tts غير مثبَّتة (محلياً بدون GPU) أو فشل
    التوليد فعلياً (تعارض إصدار numpy، نفاد ذاكرة GPU...) — المستدعي
    (router) يقرر كيف يتعامل مع الحالة (503 واضح للزبون بدل انهيار 500 خام
    بلا رسالة).

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

    try:
        inst, ref_audio, ref_text = _get_instance()
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
        logger.exception("فشل توليد الصوت — النص: %r", text[:100])
        return None
