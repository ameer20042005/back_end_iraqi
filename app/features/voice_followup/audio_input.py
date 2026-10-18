"""Native audio turns for audio-capable Gemma models; no separate ASR request."""

import base64
import io
import wave

import numpy as np

from app.features.voice_followup import vad

TRANSCRIPT_OPEN = "<customer_transcript>"
TRANSCRIPT_CLOSE = "</customer_transcript>"
MAX_AUDIO_SECONDS = 30


def audio_content(samples: np.ndarray) -> list[dict]:
    if not 0 < samples.size <= vad.SAMPLE_RATE * MAX_AUDIO_SECONDS:
        raise ValueError("Native audio turn must contain between 0 and 30 seconds")
    wav = io.BytesIO()
    with wave.open(wav, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(vad.SAMPLE_RATE)
        out.writeframes(vad.float_to_pcm16(samples))
    return [
        {"type": "input_audio", "input_audio": {
            "data": base64.b64encode(wav.getvalue()).decode("ascii"), "format": "wav"}},
        {"type": "text", "text": (
            "اسمع كلام الزبون في هذا الصوت. ابدأ جوابك بنسخ كلامه حرفياً داخل "
            "<customer_transcript>النص المسموع</customer_transcript> ثم أجب عليه حسب "
            "تعليمات المكالمة واستعمل الأدوات عند الحاجة. الوسمان للمعالجة ولا يُنطقان. "
            "لا تختلق كلاماً عند عدم وضوح الصوت.")},
    ]


class NativeTranscriptFilter:
    """Remove the transcript envelope before TTS, including split token boundaries.

    A missing envelope fails explicitly: audio cannot silently disappear from
    conversation history or be represented by an invented transcript.
    """

    def __init__(self):
        self.buffer = ""
        self.transcript = None

    def feed(self, text: str) -> str:
        if self.transcript is not None:
            return text
        self.buffer += text
        stripped = self.buffer.lstrip()
        if not (TRANSCRIPT_OPEN.startswith(stripped) or stripped.startswith(TRANSCRIPT_OPEN)):
            raise ValueError("native_transcript_missing")
        if TRANSCRIPT_CLOSE not in stripped:
            if len(stripped) > 8000:
                raise ValueError("native_transcript_too_long")
            return ""
        transcript, rest = stripped[len(TRANSCRIPT_OPEN):].split(TRANSCRIPT_CLOSE, 1)
        self.transcript = transcript.strip()
        self.buffer = ""
        return rest

    def finish(self) -> None:
        if self.transcript is None:
            raise ValueError("native_transcript_missing")
