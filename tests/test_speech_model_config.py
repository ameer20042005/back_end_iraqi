import asyncio
import io
import wave

import numpy as np
import pytest

from app.config import Settings
from app.features.order_intake import transcribe as module


def wav(seconds):
    data = io.BytesIO()
    with wave.open(data, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(np.full(int(seconds * 16000), 5000, dtype='<i2').tobytes())
    return data.getvalue()


def test_single_model_audio_and_tts_memory_defaults(monkeypatch):
    for name in ("LLM_MODEL", "TTS_POOL_SIZE", "KV_CACHE_MEMORY_GB", "VLLM_MAX_NUM_SEQS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.tts_model().repository == "ResembleAI/chatterbox"
    assert settings.model_name == "Qwen/Qwen3.5-9B"
    assert settings.stt_model == "ayoubkirouane/whisper-small-ar"
    assert settings.tts_pool_size == 1 and settings.kv_cache_memory_gb == 4 and settings.max_num_seqs == 16


def test_voice_notes_share_whisper_and_keep_all_long_recording_chunks(monkeypatch):
    seen = []

    async def transcription(samples, sample_rate):
        assert sample_rate == 16000
        seen.append(samples.size)
        text = "الطلب وصل" if len(seen) == 1 else "وصل والزبون راضي"
        return text

    monkeypatch.setattr(module.stt, "transcribe", transcription)
    assert asyncio.run(module.transcribe(wav(45))) == "الطلب وصل والزبون راضي"
    assert seen == [28 * 16000, 18 * 16000]


def test_voice_note_invalid_audio_and_truncation_fail_explicitly(monkeypatch):
    async def truncated(*args, **kwargs):
        raise ValueError("تعذّر نسخ المقطع كاملاً")

    monkeypatch.setattr(module.stt, "transcribe", truncated)
    with pytest.raises(ValueError, match="كاملاً"):
        asyncio.run(module.transcribe(wav(1)))
    with pytest.raises(ValueError):
        asyncio.run(module.decode_audio(b""))
    with pytest.raises(ValueError, match="10"):
        asyncio.run(module.decode_audio(wav(601)))


def test_whisper_transcription_errors_propagate(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("model_unavailable")

    monkeypatch.setattr(module.stt, "transcribe", broken)
    with pytest.raises(RuntimeError, match="model_unavailable"):
        asyncio.run(module.transcribe(wav(1)))
