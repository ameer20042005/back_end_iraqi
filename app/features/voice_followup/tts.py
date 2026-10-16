# -*- coding: utf-8 -*-
"""صوت الـ agent بالمكالمات الحية: Chatterbox Multilingual ومرجع صوت محلي ثابت.

**التزامن:** نسخة Chatterbox الواحدة مو آمنة للاستدعاء المتوازي — كل توليد
يعيد بناء `t3.patched_model` ويعلّق hooks على طبقات الموديل المشتركة، فمكالمتين
على نفس النسخة تخرّبان بعض. لذلك عندنا مجمّع (pool) من النسخ: كل نسخة تنطق
لمكالمة وحدة بالمرة، وعددها `settings.tts_pool_size`. النسخ تُحمَّل عند الحاجة
(أول ما تنشغل كل النسخ الموجودة) حتى ما نحجز VRAM ما نحتاجه.

**بصمة الصوت تُحسب مرة وحدة لكل نسخة** (prepare_conditionals)، لا بكل جملة:
تمرير audio_prompt_path لـ generate() يعيد قراءة الملف المرجعي كاملاً
وحساب البصمة من جديد مع كل جملة.
"""

import logging
import threading
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    _CHATTERBOX_AVAILABLE = True
except ImportError:
    _CHATTERBOX_AVAILABLE = False

from app.config import settings

logger = logging.getLogger(__name__)

# exaggeration ثابتة بكل المكالمات حتى تبقى البصمة المحسوبة صالحة (أي قيمة
# مختلفة تخلي المكتبة تعيد بناء جزء من البصمة بكل جملة).
_EXAGGERATION = 0.5
# cfg_weight أقل من الافتراضي (0.5) يبطّئ الإلقاء شوي فيصير أهدأ وأوضح بالمكالمة.
_CFG_WEIGHT = 0.4


class _VoicePool:
    """نسخ جاهزة (بأوزانها وبصمة الصوت) تُستعار وتُرجَع؛ تنتظر لو كلها مشغولة."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._free: List[object] = []
        self._created = 0
        self._cond = threading.Condition()

    def _create(self):
        import torch

        model = settings.tts_model()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("تحميل نسخة نطق %d/%d: %s (%s) على %s...",
                    self._created, self._size, model.repository, model.checkpoint, device)
        inst = ChatterboxMultilingualTTS.from_pretrained(device=device, t3_model=model.checkpoint)
        inst.prepare_conditionals(
            str(Path(__file__).resolve().parent / model.reference_audio), exaggeration=_EXAGGERATION,
        )
        return inst

    def acquire(self):
        with self._cond:
            while True:
                if self._free:
                    return self._free.pop()
                if self._created < self._size:
                    self._created += 1
                    break
                self._cond.wait()
        # التحميل خارج القفل حتى ما يوقف المكالمات اللي تنتظر نسخة جاهزة.
        try:
            return self._create()
        except Exception:
            with self._cond:
                self._created -= 1
                self._cond.notify()
            raise

    def release(self, inst) -> None:
        with self._cond:
            self._free.append(inst)
            self._cond.notify()


_pool: Optional[_VoicePool] = None
_pool_lock = threading.Lock()


def _get_pool() -> _VoicePool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _VoicePool(settings.tts_pool_size)
        return _pool


def available() -> bool:
    return _CHATTERBOX_AVAILABLE


def synthesize_array(text: str) -> Optional[Tuple["object", int]]:
    """يولّد صوت النص كمصفوفة float32 أحادية مع معدل العينات. None إذا
    المكتبة غير مثبَّتة أو النص فارغ؛ أخطاء التوليد تُرفع للمستدعي."""
    if not _CHATTERBOX_AVAILABLE or not text.strip():
        return None
    pool = _get_pool()
    inst = pool.acquire()
    try:
        wav = inst.generate(
            text,
            language_id=settings.tts_model().language,
            exaggeration=_EXAGGERATION,
            cfg_weight=_CFG_WEIGHT,
        )
        # generate() يرجع torch.Tensor بشكل (1, N) بمعدل inst.sr (24kHz).
        return wav.squeeze(0).detach().cpu().numpy(), inst.sr
    finally:
        pool.release(inst)


def synthesize(text: str) -> Optional[bytes]:
    """نفس synthesize_array لكن كملف WAV (بايتات) — يرجع None بدل رفع
    استثناء عند الفشل، ويسجّل السبب الكامل باللوق."""
    import io

    import soundfile as sf

    try:
        result = synthesize_array(text)
        if result is None:
            return None
        audio, sample_rate = result
        buf = io.BytesIO()
        sf.write(buf, audio, sample_rate, format="WAV")
        return buf.getvalue()
    except Exception:
        logger.exception("فشل توليد الصوت — النص: %r", text[:100])
        return None


def warmup() -> bool:
    """يحمّل أول نسخة (أوزان + بصمة الصوت) ويولّد جملة قصيرة قبل أول مكالمة."""
    if not _CHATTERBOX_AVAILABLE:
        return False
    if synthesize("مرحبا") is None:
        logger.warning("تعذّر التحميل المسبق لموديل النطق — سيُحمَّل بأول مكالمة")
        return False
    logger.info("✅ موديل النطق جاهز للمكالمات")
    return True
