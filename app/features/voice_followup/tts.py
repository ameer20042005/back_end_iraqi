# -*- coding: utf-8 -*-
"""صوت الـ agent بالمكالمات الحية: موديل F5-TTS العراقي ومرجع صوت محلي ثابت.

الأوزان والمفردات تُنزل من Hugging Face (settings.tts_model)، والصوت المرجعي
ونصّه الحرفي محددان بالإعدادات.

**التزامن:** عندنا مجمّع (pool) من النسخ: كل نسخة تنطق لمكالمة وحدة بالمرة،
وعددها `settings.tts_pool_size`. النسخ تُحمَّل عند الحاجة (أول ما تنشغل كل
النسخ الموجودة) حتى ما نحجز VRAM ما نحتاجه.

**التشخيص:** نتيجة آخر محاولة توليد (نجاح أو سبب الفشل) تنحفظ بـ
`_last_synthesis` ويعرضها GET /health/voice عبر `status()`.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

# أي فشل استيراد يخلّي النطق "غير مثبَّت" — مو بس ImportError: torchaudio
# المبني على torch مختلف يرمي OSError (undefined symbol) من داخل الاستيراد.
_IMPORT_ERROR: Optional[str] = None
try:
    from f5_tts.api import F5TTS

    _F5_TTS_AVAILABLE = True
except Exception as _exc:  # noqa: BLE001
    _F5_TTS_AVAILABLE = False
    _IMPORT_ERROR = type(_exc).__name__
    logging.getLogger(__name__).warning("استيراد f5_tts فشل — النطق معطّل: %s: %s",
                                        type(_exc).__name__, _exc)

from app.config import settings
from app.features.voice_followup import voice_health

logger = logging.getLogger(__name__)

# speed: 1.0 هو الافتراضي. الصوت المرجعي سريع الإلقاء (~14 حرف/ثانية) والموديل
# يقلّده، فنبطّئ لـ 0.7؛ التوليد الأطول يقلّل قصّ آخر الجملة أيضاً.
_SPEED = 0.7
# target_rms: مستوى تطبيع الصوت — الافتراضي 0.1 خافت نسبياً بمكالمة هاتفية.
_TARGET_RMS = 0.18
# nfe_step: خطوات التوليد (diffusion). 32 (الافتراضي) أسرع بالثلث من 48 بفرق
# وضوح بسيط؛ بطء التوليد هو سبب الفجوات بين الجمل بالمكالمة.
_NFE_STEP = 32


# بدائل الرموز العربية بالرموز التي يعرفها الموديل (مفردات MSA بلا «،؟؛»).
_CHAR_MAP = {"،": ",", "؟": "?", "؛": ";", "٫": ".", "…": ".", "«": "", "»": "", "ـ": ""}


def _read_vocab(path: str) -> Optional[frozenset]:
    """حروف مفردات الموديل؛ الحرف خارجها يقرؤه الموديل كمسافة."""
    try:
        return frozenset(Path(path).read_text(encoding="utf-8").split(chr(10)))
    except OSError:
        return None


def normalize_text(text: str, vocab: Optional[frozenset]) -> str:
    """يحوّل الرموز العربية لمقابلها ويحذف كل حرف ما يعرفه الموديل (تنوين،
    شدة، رموز) بدل أن ينطقه الموديل كسكتة أو صوت مشوّه."""
    out = []
    for ch in text:
        ch = _CHAR_MAP.get(ch, ch)
        if vocab is None or ch in vocab or ch == "":
            out.append(ch)
        elif ch.isspace():
            out.append(" ")
    return " ".join("".join(out).split())


def _end_with_stop(text: str) -> str:
    """نقطة بآخر النص تعطي الموديل إشارة ختام فينطق آخر كلمة كاملة بدل أن يقطعها."""
    return text if not text or text[-1] in ".?!" else text + "."


def _reference(vocab: Optional[frozenset]) -> Tuple[str, str]:
    model = settings.tts_model()
    return str(Path(__file__).resolve().parent / model.reference_audio), normalize_text(model.reference_text, vocab)


class _VoicePool:
    """نسخ جاهزة (بأوزانها) تُستعار وتُرجَع؛ تنتظر لو كلها مشغولة."""

    def __init__(self, size: int) -> None:
        self._size = size
        self._free: List[object] = []
        self._created = 0
        self._loaded = 0
        self._cond = threading.Condition()

    def _create(self):
        from huggingface_hub import hf_hub_download

        model = settings.tts_model()
        logger.info("تحميل نسخة نطق %d/%d: %s (%s)...",
                    self._created, self._size, model.repository, model.checkpoint)
        try:
            ckpt_file = hf_hub_download(repo_id=model.repository, filename=model.checkpoint)
            vocab_file = hf_hub_download(repo_id=model.repository, filename=model.vocabulary)
            inst = F5TTS(ckpt_file=ckpt_file, vocab_file=vocab_file)
        except Exception as exc:
            logger.exception("فشل تحميل نسخة النطق — %s", voice_health.describe_failure(exc))
            raise voice_health.ModelLoadError(f"تعذّر تحميل F5-TTS ({type(exc).__name__})") from exc
        # f5_tts يرقّي الموديل الرئيسي لـ float16 على GPU بقدرة ≥6 ويحوّل الـ mel
        # لـ float32 قبل الـ vocoder، بينما الـ vocoder (Vocos) أحياناً يطلع
        # float16: "mat1 and mat2 must have the same dtype". نثبّته float32.
        if hasattr(inst, "vocoder"):
            inst.vocoder = inst.vocoder.float()
        inst._text_vocab = _read_vocab(vocab_file)
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
    return _F5_TTS_AVAILABLE


def status() -> dict:
    """حالة النطق بلا تحميل: مثبَّت؟ كم نسخة محمَّلة؟ آخر محاولة توليد."""
    pool = _pool
    loaded = pool.loaded() if pool is not None else 0
    return {
        "installed": _F5_TTS_AVAILABLE,
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
    if not _F5_TTS_AVAILABLE or not text.strip():
        return None
    started = time.monotonic()
    try:
        pool = _get_pool()
        inst = pool.acquire()
        try:
            vocab = getattr(inst, "_text_vocab", None)
            ref_file, ref_text = _reference(vocab)
            wav, sample_rate, _spec = inst.infer(
                ref_file=ref_file,
                ref_text=ref_text,
                gen_text=_end_with_stop(normalize_text(text, vocab)),
                speed=_SPEED,
                target_rms=_TARGET_RMS,
                nfe_step=_NFE_STEP,
            )
            # infer() يرجع numpy float32 أحادي بمعدل 24kHz.
            result = np.asarray(wav, dtype=np.float32), sample_rate
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
    """يحمّل أول نسخة (أوزان + مرجع الصوت) ويولّد جملة قصيرة قبل أول مكالمة."""
    if not _F5_TTS_AVAILABLE:
        logger.warning("f5-tts غير مثبَّتة (%s) — ماكو نطق بالمكالمات", _IMPORT_ERROR or "ImportError")
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
