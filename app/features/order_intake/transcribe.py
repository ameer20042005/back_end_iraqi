"""Decode voice notes and transcribe chunks with the shared Arabic Voxtral."""

import asyncio
import io
import logging
import shutil
import time

import numpy as np
import soundfile as sf

from app.features.voice_followup import vad, stt

logger = logging.getLogger(__name__)
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_SECONDS = 600
CHUNK_SECONDS = 28
OVERLAP_SECONDS = 1


def _read_soundfile(data):
    with sf.SoundFile(io.BytesIO(data)) as source:
        if source.frames / source.samplerate > MAX_SECONDS:
            raise ValueError("الرسالة الصوتية أطول من 10 دقائق")
        samples = source.read(dtype="float32", always_2d=True).mean(axis=1)
        return vad.resample(samples, source.samplerate, vad.SAMPLE_RATE)


async def decode_audio(data: bytes) -> np.ndarray:
    if not data or len(data) > MAX_FILE_BYTES:
        raise ValueError("حجم الملف الصوتي غير صالح (الحد 25MB)")
    try:
        samples = await asyncio.to_thread(_read_soundfile, data)
    except (sf.LibsndfileError, RuntimeError):
        executable = shutil.which("ffmpeg")
        if not executable:
            raise ValueError("تعذّر فك الملف الصوتي؛ ثبّت ffmpeg أو أرسل WAV/OGG") from None
        process = await asyncio.create_subprocess_exec(
            executable, "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
            "-t", str(MAX_SECONDS + 1), "-f", "f32le", "-ac", "1", "-ar", "16000", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(data), 30)
        except BaseException:
            if process.returncode is None:
                process.kill()
            await process.wait()
            raise
        if process.returncode:
            raise ValueError("الملف الصوتي تالف أو صيغته غير مدعومة")
        samples = np.frombuffer(output, dtype="<f4")
    if not 0 < samples.size <= MAX_SECONDS * vad.SAMPLE_RATE or not np.isfinite(samples).all():
        raise ValueError("مدة الملف الصوتي غير صالحة (الحد 10 دقائق)")
    return samples


def merge_transcript(previous: str, current: str) -> str:
    left, right = previous.split(), current.split()
    overlap = 0
    for n in range(1, min(40, len(left), len(right)) + 1):
        if left[-n:] == right[:n]:
            overlap = n
    return " ".join(left + right[overlap:])


async def transcribe(audio_bytes: bytes) -> str:
    started = time.monotonic()
    chunks, transcript, status = 0, "", "error"
    try:
        samples = await decode_audio(audio_bytes)
        size = CHUNK_SECONDS * vad.SAMPLE_RATE
        step = (CHUNK_SECONDS - OVERLAP_SECONDS) * vad.SAMPLE_RATE
        for start in range(0, samples.size, step):
            part = samples[start:start + size]
            text = await stt.transcribe(part, vad.SAMPLE_RATE)
            transcript = merge_transcript(transcript, text.strip())
            chunks += 1
            if start + size >= samples.size:
                break
        status = "ok" if transcript else "empty"
        return transcript
    finally:
        logger.info("audio_transcription_end engine=voxtral status=%s chunks=%d elapsed_ms=%d",
                    status, chunks, int((time.monotonic() - started) * 1000))
