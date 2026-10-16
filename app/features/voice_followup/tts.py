# -*- coding: utf-8 -*-
"""توليد صوت المتابعة العربي بموديل Chatterbox Multilingual ومرجع صوت محلي ثابت.

تُنزل الأوزان من Hugging Face أول مرة وتُعاد الاستفادة من نسخة واحدة.
الصوت المرجعي (voice cloning) محدد في إعدادات التطبيق؛ Chatterbox ما يحتاج
نص المرجع، ويأخذ أول ~10 ثوانٍ من المقطع فقط للتكييف.
"""

import logging
from pathlib import Path
from typing import Optional

try:
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    _CHATTERBOX_AVAILABLE = True
except ImportError:
    _CHATTERBOX_AVAILABLE = False

from app.config import settings

logger = logging.getLogger(__name__)

_instance = None


def _get_instance() -> tuple:
    """تحميل نسخة النطق العربي مع مسار مرجعها مرة واحدة."""
    global _instance
    if _instance is not None:
        return _instance

    import torch

    model = settings.tts_model()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("تحميل موديل النطق %s (%s) على %s (أول مرة قد تستغرق دقائق للتنزيل)...",
                model.repository, model.checkpoint, device)
    inst = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model=model.checkpoint)
    ref_audio = str(Path(__file__).resolve().parent / model.reference_audio)

    _instance = (inst, ref_audio, model.language)
    return _instance


def warmup() -> bool:
    """يحمّل موديل النطق **العربي** (أوزان + مرجع الصوت) عند إقلاع الخادم
    بدل أول طلب حقيقي — نفس مبرر transcribe.warmup (انظر هناك). يرجع True
    إن جهز الموديل فعلاً."""
    if not _CHATTERBOX_AVAILABLE:
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
    """يحوّل النص لملف صوتي WAV (بايتات) بالصوت المرجعي. يرجع None إذا مكتبة
    chatterbox-tts غير مثبَّتة (محلياً بدون GPU) أو فشل التوليد فعلياً (نفاد
    ذاكرة GPU، تعارض إصدارات...) — المستدعي (router) يقرر كيف يتعامل مع
    الحالة (503 واضح للزبون بدل انهيار 500 خام بلا رسالة).

    ⚠️ استثناءات generate() تُلتقط هنا صراحةً وتُسجَّل بالتفصيل (traceback
    كامل) بدل ما تتسرّب كـ500 غير مفسَّر لـFastAPI، حتى يظهر السبب الحقيقي
    بـ/tmp/api.log فوراً."""
    if not _CHATTERBOX_AVAILABLE:
        return None
    if not text.strip():
        return None

    import io

    import soundfile as sf

    try:
        inst, ref_audio, language = _get_instance()
        wav = inst.generate(
            text,
            language_id=language,
            audio_prompt_path=ref_audio,
            # الافتراضيات (0.5/0.5) موصى بيها لأغلب الحالات. cfg_weight أقل
            # شوي يبطّئ الإلقاء ويخليه أهدأ وأوضح بمكالمة متابعة، وexaggeration
            # بالافتراضي حتى ما يصير الأداء مبالغ أو سريع.
            exaggeration=0.5,
            cfg_weight=0.4,
        )
        # generate() يرجع torch.Tensor بشكل (1, N) بمعدل inst.sr (24kHz).
        audio = wav.squeeze(0).detach().cpu().numpy()
        buf = io.BytesIO()
        sf.write(buf, audio, inst.sr, format="WAV")
        return buf.getvalue()
    except Exception:
        logger.exception("فشل توليد الصوت — النص: %r", text[:100])
        return None
