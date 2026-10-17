# -*- coding: utf-8 -*-
"""كشف الكلام وتقطيع الأدوار لصوت الزبون الواصل عبر WebSocket (stream.py).

مسار LiveKit عنده VAD جاهز (silero عبر plugin)؛ هنا ما عدنا جلسة LiveKit،
فنستعمل نفس أوزان Silero مباشرة بـ onnxruntime (ملف الأوزان من حزمة
livekit-plugins-silero بلا ما نستورد livekit.agents)، وإذا ما متوفرة نرجع
لكاشف طاقة بسيط.

الصوت يدخل PCM float32 أحادي بـ 16kHz (stream.py يحوّله)، ويطلع أحداث:
  - ("start", None): الزبون بدأ يحچي (كلام متواصل ≥ min_speech_ms) — يستعمل للمقاطعة.
  - ("end", samples): الزبون سكت ≥ end_silence_ms — المقطع كاملاً للنسخ.
"""

import importlib.util
import logging
import math
import threading
from collections import deque
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

from app.config import settings

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000

Event = Tuple[str, Optional[np.ndarray]]


# ---------------------------------------------------------------------------
# تحويلات الصوت
# ---------------------------------------------------------------------------

def pcm16_to_float(data: bytes) -> np.ndarray:
    if len(data) % 2:
        data = data[:-1]
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm16(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """تحويل معدل العينات بنسبة كسرية (up/down) مع فلتر تمرير منخفض (windowed
    sinc) قبل التنعيم حتى ما يصير aliasing — بديل numpy عن scipy.resample_poly."""
    samples = np.asarray(samples, dtype=np.float32)
    if src_rate == dst_rate or samples.size == 0:
        return samples
    g = math.gcd(src_rate, dst_rate)
    up, down = dst_rate // g, src_rate // g
    half = 10 * max(up, down)
    n = np.arange(-half, half + 1)
    cutoff = 1.0 / max(up, down)
    taps = cutoff * np.sinc(cutoff * n) * np.kaiser(2 * half + 1, 5.0) * up
    stuffed = np.zeros(samples.size * up, dtype=np.float32)
    stuffed[::up] = samples
    filtered = np.convolve(stuffed, taps.astype(np.float32), mode="same")
    return filtered[::down].astype(np.float32)


class Agc:
    """تطبيع مستوى بسيط قبل VAD وWhisper: يرفع الصوت الواطي (بعد ترميز
    الواتساب أو مايك بعيد) لحد target_dbfs، بلا خفض أبداً وبسقف max_gain_db.

    المستوى المتتبَّع = أعلى RMS لنوافذ 20ms (ذروة الكلام، مو متوسط يخفّفه
    السكوت)، يرتفع فوراً وينزل ببطء (release_seconds) حتى ما يضخّم الضوضاء
    بين الكلمات. كاشف الطاقة يحتاجه (يفقد الكلام تحت ~-45 dBFS)؛ Silero
    ثابت تقريباً مع المستوى، وWhisper يستفيد من مقطع بمستوى طبيعي."""

    _WINDOW = 320  # 20ms

    def __init__(self, target_dbfs: float = -20.0, max_gain_db: float = 20.0,
                 release_seconds: float = 3.0) -> None:
        self._target = 10.0 ** (target_dbfs / 20.0)
        self._max_gain = 10.0 ** (max_gain_db / 20.0)
        self._release = release_seconds
        self._level = 0.0
        self.gain = 1.0

    def process(self, samples: np.ndarray) -> np.ndarray:
        if samples.size == 0:
            return samples
        n = samples.size // self._WINDOW
        if n:
            frames = samples[:n * self._WINDOW].reshape(n, self._WINDOW).astype(np.float64)
            rms = float(np.sqrt(np.mean(np.square(frames), axis=1)).max())
        else:
            rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
        decay = math.exp(-(samples.size / SAMPLE_RATE) / self._release)
        self._level = max(rms, self._level * decay)
        self.gain = min(self._max_gain, max(1.0, self._target / max(self._level, 1e-9)))
        if self.gain == 1.0:
            return samples
        return np.clip(samples * self.gain, -1.0, 1.0).astype(np.float32)


def make_agc() -> Optional[Agc]:
    if not settings.voice_stream_agc:
        return None
    return Agc(settings.voice_stream_agc_target_dbfs, settings.voice_stream_agc_max_gain_db)


# ---------------------------------------------------------------------------
# مصنّفات الإطار: كلام أو لا
# ---------------------------------------------------------------------------

class EnergyClassifier:
    """كلام = طاقة الإطار (RMS) فوق عتبة بالـ dBFS. بلا تبعيات."""

    kind = "energy"
    window = 320  # 20ms

    def __init__(self, threshold_dbfs: float) -> None:
        self._threshold = threshold_dbfs

    def __call__(self, frame: np.ndarray) -> bool:
        rms = float(np.sqrt(np.mean(np.square(frame), dtype=np.float64)))
        return 20.0 * math.log10(rms + 1e-9) >= self._threshold


_silero_session = None
_silero_lock = threading.Lock()


def _silero_model_path() -> Optional[Path]:
    # find_spec ما ينفّذ __init__ الحزمة (اللي يستورد livekit.agents).
    try:
        spec = importlib.util.find_spec("livekit.plugins.silero")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    path = Path(list(spec.submodule_search_locations)[0]) / "resources" / "silero_vad.onnx"
    return path if path.is_file() else None


def _get_silero_session():
    """جلسة ONNX وحدة مشتركة لكل المكالمات (run آمنة متوازياً)؛ الحالة لكل مكالمة."""
    global _silero_session
    with _silero_lock:
        if _silero_session is None:
            import onnxruntime

            path = _silero_model_path()
            if path is None:
                raise FileNotFoundError("أوزان silero_vad.onnx غير موجودة (livekit-plugins-silero)")
            opts = onnxruntime.SessionOptions()
            opts.inter_op_num_threads = 1
            opts.intra_op_num_threads = 1
            _silero_session = onnxruntime.InferenceSession(
                str(path), providers=["CPUExecutionProvider"], sess_options=opts,
            )
        return _silero_session


class SileroClassifier:
    """نفس نموذج Silero اللي يستعمله الـ agent بـ LiveKit، بحالة RNN خاصة بكل مكالمة."""

    kind = "silero"
    window = 512  # 32ms بـ 16kHz
    _context = 64

    def __init__(self, threshold: float) -> None:
        self._sess = _get_silero_session()
        self._threshold = threshold
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._buffer = np.zeros((1, self._context + self.window), dtype=np.float32)

    def __call__(self, frame: np.ndarray) -> bool:
        self._buffer[:, self._context:] = frame
        out, self._state = self._sess.run(None, {"input": self._buffer, "state": self._state, "sr": self._sr})
        self._buffer[:, :self._context] = self._buffer[:, -self._context:]
        return float(out.item()) >= self._threshold


def make_classifier():
    mode = settings.voice_stream_vad
    if mode in ("auto", "silero"):
        try:
            return SileroClassifier(settings.voice_stream_silero_threshold)
        except Exception:
            if mode == "silero":
                raise
            logger.warning("Silero VAD غير متوفر؛ استعمال كاشف الطاقة", exc_info=True)
    return EnergyClassifier(settings.voice_stream_energy_threshold_dbfs)


# ---------------------------------------------------------------------------
# تقطيع الأدوار
# ---------------------------------------------------------------------------

class Endpointer:
    """يحوّل تيار PCM لأحداث start/end. المقطع يشمل ~300ms قبل بداية الكلام
    (preroll) حتى ما ينقص أول حرف بالنسخ."""

    def __init__(self, classifier, min_speech_ms: Optional[int] = None,
                 end_silence_ms: Optional[int] = None, max_segment_seconds: Optional[float] = None,
                 preroll_ms: int = 300) -> None:
        self._classify = classifier
        self._win = classifier.window
        win_ms = 1000.0 * self._win / SAMPLE_RATE
        min_speech_ms = settings.voice_stream_min_speech_ms if min_speech_ms is None else min_speech_ms
        end_silence_ms = settings.voice_stream_end_silence_ms if end_silence_ms is None else end_silence_ms
        max_segment_seconds = (settings.voice_stream_max_segment_seconds
                               if max_segment_seconds is None else max_segment_seconds)
        self._min_speech = max(1, math.ceil(min_speech_ms / win_ms))
        self._end_silence = max(1, math.ceil(end_silence_ms / win_ms))
        self._max_windows = max(self._min_speech + 1, int(max_segment_seconds * 1000 / win_ms))
        self._preroll = deque(maxlen=self._min_speech + max(0, int(preroll_ms / win_ms)))
        self._pending = np.zeros(0, dtype=np.float32)
        self._segment: List[np.ndarray] = []
        self._speech_run = 0
        self._silence_run = 0
        self.in_speech = False
        # عدّادات تراكمية للإحصاءات (ما تتصفّر مع reset).
        self.windows = 0
        self.speech_windows = 0
        self.starts = 0

    def reset(self) -> None:
        self._preroll.clear()
        self._segment = []
        self._speech_run = self._silence_run = 0
        self.in_speech = False

    def push(self, samples: np.ndarray) -> List[Event]:
        events: List[Event] = []
        data = np.concatenate([self._pending, samples]) if self._pending.size else samples
        full = data.size - data.size % self._win
        for i in range(0, full, self._win):
            event = self._step(data[i:i + self._win])
            if event:
                events.append(event)
        self._pending = data[full:].copy()
        return events

    def _step(self, frame: np.ndarray) -> Optional[Event]:
        speech = self._classify(frame)
        self.windows += 1
        self.speech_windows += bool(speech)
        if not self.in_speech:
            self._preroll.append(frame)
            self._speech_run = self._speech_run + 1 if speech else 0
            if self._speech_run >= self._min_speech:
                self.in_speech = True
                self._segment = list(self._preroll)
                self._preroll.clear()
                self._silence_run = 0
                self.starts += 1
                return ("start", None)
            return None
        self._segment.append(frame)
        self._silence_run = 0 if speech else self._silence_run + 1
        if self._silence_run >= self._end_silence or len(self._segment) >= self._max_windows:
            segment = np.concatenate(self._segment)
            self.reset()
            return ("end", segment)
        return None
