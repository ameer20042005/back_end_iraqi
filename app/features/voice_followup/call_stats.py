# -*- coding: utf-8 -*-
"""إحصاءات صوت الزبون لكل مكالمة WebSocket — أرقام بس، بلا نص ولا بيانات شخصية.

تجاوب على سؤال "ليش الـ agent ما يسمع الزبون؟" من اللوق أو GET /health/voice
(`last_call`)، عبر حقل `diagnosis`:

    no_audio              ماكو صوت واصل أصلاً (أو كله قبل accepted)
    sample_rate_mismatch  معدل الصوت الفعلي بعيد عن sample_rate المعلن
    too_quiet             الصوت واصل بس مستواه واطي جداً وVAD ما لگى كلام
    vad_no_speech         الصوت مسموع (مستوى معقول) بس VAD ما صنّف ولا نافذة كلام
    stt_error             Whisper يرمي استثناء لكل مقطع
    stt_empty         مقاطع تروح لـ Whisper وترجع نص فارغ
    no_speech_yet         المكالمة بعدها، ماكو كلام لحد الآن
    ok                    وصل نص من الزبون
"""

import math
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

LEVEL_WINDOW = 320  # 20ms بـ 16kHz
SILENCE_DBFS = -100.0
# أقل من هذا (أعلى مستوى بالمكالمة) = "واطي جداً" — كاشف الطاقة الافتراضي (-40) ما يلگاه.
TOO_QUIET_DBFS = -50.0


def _round(value: Optional[float], digits: int = 1) -> Optional[float]:
    return None if value is None else round(value, digits)


class CallAudioStats:
    def __init__(self, sample_rate: int, vad_kind: str, agc: bool) -> None:
        self.sample_rate = sample_rate
        self.vad_kind = vad_kind
        self.agc = agc
        self.started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._started = time.monotonic()
        self._first_audio: Optional[float] = None
        self._last_audio: Optional[float] = None
        self.audio_bytes = 0
        self.ignored_bytes = 0
        self.audio_samples = 0  # بعد التحويل لـ 16kHz
        self._level_sum = 0.0
        self._level_n = 0
        self.level_max: Optional[float] = None
        self._gain_sum = 0.0
        self._gain_n = 0
        self.gain_db_max = 0.0
        self.vad_windows = 0
        self.vad_speech_windows = 0
        self.turn_starts = 0
        self.segments = 0
        self.segment_samples = 0
        self.stt_empty = 0
        self.stt_errors = 0
        self.stt_error_type: Optional[str] = None
        self.user_transcripts = 0
        self.interrupts = 0
        self.ignored_short = 0
        self.echo_dropped = 0
        self.protocol_errors = 0

    # ── التسجيل ─────────────────────────────────────────────────────────────

    def on_ignored(self, nbytes: int) -> None:
        self.ignored_bytes += nbytes

    def on_audio(self, nbytes: int, samples: np.ndarray) -> None:
        """samples: الصوت الخام بـ 16kHz قبل AGC — المستوى الحقيقي الواصل."""
        now = time.monotonic()
        if self._first_audio is None:
            self._first_audio = now
        self._last_audio = now
        self.audio_bytes += nbytes
        self.audio_samples += samples.size
        n = samples.size // LEVEL_WINDOW
        if n == 0:
            return
        frames = samples[:n * LEVEL_WINDOW].reshape(n, LEVEL_WINDOW).astype(np.float64)
        rms = np.sqrt(np.mean(np.square(frames), axis=1))
        db = np.maximum(20.0 * np.log10(rms + 1e-12), SILENCE_DBFS)
        self._level_sum += float(db.sum())
        self._level_n += n
        peak = float(db.max())
        self.level_max = peak if self.level_max is None else max(self.level_max, peak)

    def on_gain(self, gain: float) -> None:
        db = 20.0 * math.log10(max(gain, 1e-9))
        self._gain_sum += db
        self._gain_n += 1
        self.gain_db_max = max(self.gain_db_max, db)

    def on_vad(self, windows: int, speech_windows: int, starts: int) -> None:
        self.vad_windows, self.vad_speech_windows, self.turn_starts = windows, speech_windows, starts

    def on_segment(self, samples: int) -> None:
        self.segments += 1
        self.segment_samples += samples

    def on_transcript(self, text: str) -> None:
        if text:
            self.user_transcripts += 1
        else:
            self.stt_empty += 1

    def on_stt_error(self, exc: BaseException) -> None:
        self.stt_errors += 1
        self.stt_error_type = type(exc).__name__

    # ── القراءة ────────────────────────────────────────────────────────────

    def measured_sample_rate(self) -> Optional[int]:
        """معدل العينات المستنتج من سرعة وصول البايتات (التطبيق يبث بالوقت الحقيقي)."""
        if self._first_audio is None or self._last_audio is None:
            return None
        span = self._last_audio - self._first_audio
        if span < 2.0:
            return None
        return int(round(self.audio_bytes / 2 / span, -2))

    def diagnosis(self) -> str:
        if self.user_transcripts:
            return "ok"
        if self.audio_bytes == 0:
            return "no_audio"
        measured = self.measured_sample_rate()
        if measured and abs(measured - self.sample_rate) > 0.25 * self.sample_rate:
            return "sample_rate_mismatch"
        if self.segments and self.stt_errors >= self.segments:
            return "stt_error"
        if self.segments and self.stt_empty:
            return "stt_empty"
        if self.vad_windows and self.vad_speech_windows == 0:
            if self.level_max is not None and self.level_max < TOO_QUIET_DBFS:
                return "too_quiet"
            return "vad_no_speech"
        return "no_speech_yet"

    def snapshot(self, active: bool = True) -> dict:
        return {
            "active": active,
            "started_at": self.started_at,
            "duration_s": _round(time.monotonic() - self._started),
            "sample_rate": self.sample_rate,
            "measured_sample_rate": self.measured_sample_rate(),
            "vad": self.vad_kind,
            "agc": self.agc,
            "audio_bytes": self.audio_bytes,
            "audio_seconds": _round(self.audio_samples / 16000),
            "ignored_bytes": self.ignored_bytes,
            "level_dbfs_mean": _round(self._level_sum / self._level_n) if self._level_n else None,
            "level_dbfs_max": _round(self.level_max),
            "agc_gain_db_mean": _round(self._gain_sum / self._gain_n) if self._gain_n else None,
            "agc_gain_db_max": _round(self.gain_db_max),
            "vad_windows": self.vad_windows,
            "vad_speech_windows": self.vad_speech_windows,
            "turn_starts": self.turn_starts,
            "segments_to_model": self.segments,
            "segment_seconds": _round(self.segment_samples / 16000),
            "stt_empty": self.stt_empty,
            "stt_errors": self.stt_errors,
            "stt_error_type": self.stt_error_type,
            "user_transcripts": self.user_transcripts,
            "interrupts": self.interrupts,
            "ignored_short": self.ignored_short,
            "echo_dropped": self.echo_dropped,
            "protocol_errors": self.protocol_errors,
            "diagnosis": self.diagnosis(),
        }

    def log_line(self, active: bool = True) -> str:
        """سطر لوق واحد key=value (بلا started_at)."""
        snap = self.snapshot(active)
        snap.pop("started_at")
        return " ".join(f"{k}={v}" for k, v in snap.items())
