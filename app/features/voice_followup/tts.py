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

**التشخيص:** نتيجة آخر محاولة توليد (نجاح أو سبب الفشل) تنحفظ بـ
`_last_synthesis` ويعرضها GET /health/voice عبر `status()`.
"""

import inspect
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# أي فشل استيراد يخلّي النطق "غير مثبَّت" — مو بس ImportError: torchaudio
# المبني على torch مختلف يرمي OSError (undefined symbol) من داخل الاستيراد.
_IMPORT_ERROR: Optional[str] = None
try:
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS

    _CHATTERBOX_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _CHATTERBOX_AVAILABLE = False
    _IMPORT_ERROR = type(_exc).__name__
    logging.getLogger(__name__).warning("استيراد chatterbox فشل — النطق معطّل: %s: %s",
                                        type(_exc).__name__, _exc)

from app.config import settings
from app.features.voice_followup import voice_health

logger = logging.getLogger(__name__)

# exaggeration ثابتة بكل المكالمات حتى تبقى البصمة المحسوبة صالحة (أي قيمة
# مختلفة تخلي المكتبة تعيد بناء جزء من البصمة بكل جملة).
_EXAGGERATION = 0.5
# Voice-agent defaults: avoid deliberately slowing every utterance.
_CFG_WEIGHT = 0.5


def _load_kwargs(device: str, checkpoint: str) -> dict:
    """chatterbox-tts==0.1.7 (آخر نسخة على PyPI) تعرّف from_pretrained(device)
    بس، فتمرير t3_model لها يرمي TypeError بكل محاولة تحميل. نمرّره بس لو
    المكتبة المثبَّتة تقبله، وإلا تنحمّل نقطة التفتيش الافتراضية بالمكتبة."""
    kwargs = {"device": device}
    try:
        params = inspect.signature(ChatterboxMultilingualTTS.from_pretrained).parameters
    except (TypeError, ValueError):
        params = {}
    if "t3_model" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        kwargs["t3_model"] = checkpoint
    elif checkpoint:
        logger.warning("chatterbox-tts المثبَّتة ما تدعم اختيار نقطة التفتيش (t3_model=%r) — "
                       "تنحمّل نقطة التفتيش الافتراضية بالمكتبة", checkpoint)
    return kwargs


class _VoicePool:
    """نسخ جاهزة (بأوزانها وبصمة الصوت) تُستعار وتُرجَع؛ تنتظر لو كلها مشغولة."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._free: List[object] = []
        self._created = 0
        self._loaded = 0
        self._cond = threading.Condition()

    def _create(self):
        import torch

        model = settings.tts_model()
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info("تحميل نسخة نطق %d/%d: %s (%s) على %s...",
                    self._created, self._size, model.repository, model.checkpoint, device)
        try:
            inst = ChatterboxMultilingualTTS.from_pretrained(**_load_kwargs(device, model.checkpoint))
            inst.prepare_conditionals(
                str(Path(__file__).resolve().parent / model.reference_audio), exaggeration=_EXAGGERATION,
            )
        except Exception as exc:
            logger.exception("فشل تحميل نسخة النطق — %s", voice_health.describe_failure(exc))
            raise voice_health.ModelLoadError(f"تعذّر تحميل Chatterbox ({type(exc).__name__})") from exc
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
            inst = self._create()
        except Exception:
            with self._cond:
                self._created -= 1
                self._cond.notify()
            raise
        with self._cond:
            self._loaded += 1
        return inst

    def release(self, inst) -> None:
        with self._cond:
            self._free.append(inst)
            self._cond.notify()

    def loaded(self) -> int:
        with self._cond:
            return self._loaded


_pool: Optional[_VoicePool] = None
_pool_lock = threading.Lock()
_last_synthesis: Optional[dict] = None


def _get_pool() -> _VoicePool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = _VoicePool(settings.tts_pool_size)
        return _pool


def _record(ok: bool, started: float, exc: Optional[BaseException] = None) -> None:
    global _last_synthesis
    _last_synthesis = {
        "ok": ok,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - started, 2),
        "reason": None if ok else voice_health.classify_error(exc),
        "error_type": type(exc).__name__ if exc is not None else None,
    }


def available() -> bool:
    return _CHATTERBOX_AVAILABLE


def status() -> dict:
    """حالة النطق بلا تحميل: مثبَّت؟ كم نسخة محمَّلة؟ آخر محاولة توليد."""
    pool = _pool
    loaded = pool.loaded() if pool is not None else 0
    return {
        "installed": _CHATTERBOX_AVAILABLE,
        "import_error": _IMPORT_ERROR,
        "loaded": loaded > 0,
        "instances": loaded,
        "pool_size": settings.tts_pool_size,
        "last_synthesis": dict(_last_synthesis) if _last_synthesis else None,
    }


def synthesize_array(text: str) -> Optional[Tuple["object", int]]:
    """يولّد صوت النص كمصفوفة float32 أحادية مع معدل العينات. None إذا
    المكتبة غير مثبَّتة أو النص فارغ؛ أخطاء التحميل/التوليد تُرفع للمستدعي
    (ModelLoadError لو فشل التحميل، والاستثناء الأصلي لو فشل التوليد)."""
    if not _CHATTERBOX_AVAILABLE or not text.strip():
        return None
    started = time.monotonic()
    try:
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
            result = wav.squeeze(0).detach().cpu().numpy(), inst.sr
        finally:
            pool.release(inst)
    except Exception as exc:
        _record(False, started, exc)
        raise
    _record(True, started)
    return result


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
    except Exception as exc:
        logger.exception("فشل توليد الصوت — النص: %r — %s", text[:100], voice_health.describe_failure(exc))
        return None


def warmup() -> bool:
    """يحمّل أول نسخة (أوزان + بصمة الصوت) ويولّد جملة قصيرة قبل أول مكالمة."""
    if not _CHATTERBOX_AVAILABLE:
        logger.warning("chatterbox-tts غير مثبَّتة (%s) — ماكو نطق بالمكالمات", _IMPORT_ERROR or "ImportError")
        return False
    # synthesize_array مباشرة (مو synthesize): التحميل المسبق ما يحتاج WAV،
    # فما يعتمد على soundfile/libsndfile.
    try:
        ok = synthesize_array("مرحبا") is not None
    except Exception as exc:
        logger.exception("فشل التحميل المسبق لموديل النطق — %s", voice_health.describe_failure(exc))
        ok = False
    if not ok:
        logger.warning("تعذّر التحميل المسبق لموديل النطق — سيُحمَّل بأول مكالمة")
        return False
    logger.info("✅ موديل النطق جاهز للمكالمات")
    return True
