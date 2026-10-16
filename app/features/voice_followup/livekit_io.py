# -*- coding: utf-8 -*-
"""ربط موديلاتنا المحلية بواجهات LiveKit Agents: Whisper كـ STT وChatterbox كـ TTS.

كلاهما غير متدفق (non-streaming)، وLiveKit يغلّفهما تلقائياً:
  - STT: يقطع الـ VAD كلام الزبون عند سكوته، ويرسل المقطع كاملاً لـ recognize().
  - TTS: يقطع رد الموديل لجمل، ويولّد كل جملة بـ synthesize() فيبدأ الكلام
    بأول جملة بدل انتظار الرد كاملاً.
الموديلان متزامنان (GPU)، فيشتغلان بخيط منفصل حتى ما يوقفان حلقة الأحداث.
"""

import asyncio
import uuid

import numpy as np
from livekit import rtc
from livekit.agents import (
    DEFAULT_API_CONNECT_OPTIONS,
    NOT_GIVEN,
    APIConnectOptions,
    APIError,
    NotGivenOr,
    stt,
    tts,
)
from livekit.agents.utils import AudioBuffer

from app.features.voice_followup import stt as whisper
from app.features.voice_followup import tts as chatterbox

_LANGUAGE = "ar"
# معدل مخرجات Chatterbox الثابت (S3GEN_SR).
_TTS_SAMPLE_RATE = 24000


def _to_mono_float(frame: rtc.AudioFrame) -> np.ndarray:
    samples = np.frombuffer(frame.data, dtype=np.int16).astype(np.float32) / 32768.0
    if frame.num_channels > 1:
        samples = samples.reshape(-1, frame.num_channels).mean(axis=1)
    return samples


class WhisperSTT(stt.STT):
    def __init__(self) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))

    @property
    def model(self) -> str:
        return "whisper"

    @property
    def provider(self) -> str:
        return "local"

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        frame = rtc.combine_audio_frames(buffer)
        samples = _to_mono_float(frame)
        text = await asyncio.to_thread(whisper.transcribe_pcm, samples, frame.sample_rate)
        if text is None:
            raise APIError("موديل النسخ غير مثبَّت (transformers)", retryable=False)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            alternatives=[stt.SpeechData(language=_LANGUAGE, text=text)],
        )


class ChatterboxTTS(tts.TTS):
    def __init__(self) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=_TTS_SAMPLE_RATE,
            num_channels=1,
        )

    @property
    def model(self) -> str:
        return "chatterbox-multilingual"

    @property
    def provider(self) -> str:
        return "local"

    def synthesize(
        self, text: str, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> "ChatterboxChunkedStream":
        return ChatterboxChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class ChatterboxChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        result = await asyncio.to_thread(chatterbox.synthesize_array, self.input_text)
        if result is None:
            raise APIError("موديل النطق غير مثبَّت (chatterbox-tts)", retryable=False)
        audio, sample_rate = result
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
        output_emitter.initialize(
            request_id=uuid.uuid4().hex,
            sample_rate=sample_rate,
            num_channels=1,
            mime_type="audio/pcm",
        )
        output_emitter.push(pcm)
        output_emitter.flush()
