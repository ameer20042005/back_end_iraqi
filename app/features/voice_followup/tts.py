# -*- coding: utf-8 -*-
"""تحويل نص عراقي لصوت — موديل ameer4wisam/Habibi-TTS-IRQ (نسخة عراقية
مخصَّصة من F5-TTS، انظر بطاقة الموديل على Hugging Face: base model
SWivid/F5-TTS). خلافاً لـ Whisper (app/features/order_intake/transcribe.py)،
هذا **ليس** موديل transformers.pipeline قياسي — هو موديل zero-shot voice
cloning يحتاج ثلاثة مدخلات لكل توليد:

    ref_audio  — مقطع صوتي مرجعي بصوت المتكلم المطلوب تقليده (assets/IRQ.wav
                 المرفق بهذا المشروع — نسخة محلية بـ reference/IRQ.wav).
    ref_text   — النص الحرفي المنطوق بـ ref_audio (ثابت، مطابق للتسجيل).
    gen_text   — النص المطلوب توليد صوت له (سؤال المتابعة أو الشكر).

نقطة التفتيش (model_100000.safetensors + vocab.txt) تحت
Specialized/IRQ/ بمستودع الموديل على HF — تُنزَّل مرة واحدة عبر
huggingface_hub وتُخزَّن بالكاش المحلي القياسي لـ HF (~/.cache/huggingface).

يُستخدم حصراً بمسار المتابعة الصوتية (app/features/voice_followup/router.py).
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

_HF_REPO_ID = "ameer4wisam/Habibi-TTS-IRQ"
_HF_CKPT_PATH = "Specialized/IRQ/model_100000.safetensors"
_HF_VOCAB_PATH = "Specialized/IRQ/vocab.txt"

# الصوت المرجعي ونصّه الحرفي المطابق — ثابتان لهذا الموديل بالذات (نسخة صوت
# واحدة مدرَّبة عليها)، لا يتغيران بين الطلبات. النص هنا هو بالضبط ما يُنطق
# بملف reference/IRQ.wav المرفق.
_REF_AUDIO_PATH = Path(__file__).resolve().parent / "reference" / "IRQ.wav"
_REF_TEXT = "اا ما نقدر ناخذ وقت أكثر، ااا لأنه شروط كلش يحتاجلها وقت."

_tts_instance: Optional["F5TTS"] = None


def _get_instance() -> "F5TTS":
    global _tts_instance
    if _tts_instance is None:
        from huggingface_hub import hf_hub_download

        logger.info(
            "تحميل موديل تحويل النص لصوت %s (أول مرة قد تستغرق دقائق للتنزيل)...",
            _HF_REPO_ID,
        )
        ckpt_file = hf_hub_download(repo_id=_HF_REPO_ID, filename=_HF_CKPT_PATH)
        vocab_file = hf_hub_download(repo_id=_HF_REPO_ID, filename=_HF_VOCAB_PATH)
        _tts_instance = F5TTS(ckpt_file=ckpt_file, vocab_file=vocab_file)
    return _tts_instance


def warmup() -> bool:
    """يحمّل موديل TTS (أوزان + مرجع الصوت) عند إقلاع الخادم بدل أول طلب
    حقيقي — نفس مبرر transcribe.warmup (انظر هناك). يرجع True إن جهز
    الموديل فعلاً."""
    if not _F5_TTS_AVAILABLE:
        return False
    try:
        synthesize("مرحبا")
        logger.info("✅ موديل تحويل النص لصوت جاهز (تحميل مسبق مكتمل)")
        return True
    except Exception:
        logger.warning("تعذّر التحميل المسبق لموديل TTS — سيُحمَّل عند أول طلب", exc_info=True)
        return False


def synthesize(text: str) -> Optional[bytes]:
    """يحوّل نصاً عراقياً لملف صوتي WAV (بايتات) بصوت المرجع المرفق. يرجع
    None إذا مكتبة f5_tts غير مثبَّتة (محلياً بدون GPU) — المستدعي (router)
    يقرر كيف يتعامل مع الحالة (503 للزبون بدل توليد صوت فارغ)."""
    if not _F5_TTS_AVAILABLE:
        return None
    if not text.strip():
        return None

    import io

    import soundfile as sf

    wav, sr, _spec = _get_instance().infer(
        ref_file=str(_REF_AUDIO_PATH),
        ref_text=_REF_TEXT,
        gen_text=text,
    )
    buf = io.BytesIO()
    sf.write(buf, wav, sr, format="WAV")
    return buf.getvalue()
