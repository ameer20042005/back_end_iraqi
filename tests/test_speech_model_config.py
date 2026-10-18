import asyncio
import base64
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
    for name in ("TTS_POOL_SIZE", "KV_CACHE_MEMORY_GB", "VLLM_MAX_NUM_SEQS"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings()
    assert settings.tts_model().repository == "ResembleAI/chatterbox"
    assert settings.tts_pool_size == 1 and settings.kv_cache_memory_gb == 4 and settings.max_num_seqs == 16


def test_voice_notes_use_native_audio_and_keep_all_long_recording_chunks(monkeypatch):
    seen = []

    async def completion(messages, **kwargs):
        encoded = messages[0]["content"][0]["input_audio"]["data"]
        with wave.open(io.BytesIO(base64.b64decode(encoded))) as audio:
            seen.append(audio.getnframes())
        assert kwargs["generation_options"]["chat_template_kwargs"] == {"enable_thinking": False}
        text = "الطلب وصل" if len(seen) == 1 else "وصل والزبون راضي"
        return {"choices": [{"finish_reason": "stop", "message": {"content": text}}]}

    monkeypatch.setattr(module.llm_engine, "create_chat_completion", completion)
    assert asyncio.run(module.transcribe(wav(45))) == "الطلب وصل والزبون راضي"
    assert seen == [28 * 16000, 18 * 16000]


def test_voice_note_invalid_audio_and_truncation_fail_explicitly(monkeypatch):
    async def truncated(*args, **kwargs):
        return {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}

    monkeypatch.setattr(module.llm_engine, "create_chat_completion", truncated)
    with pytest.raises(ValueError, match="كاملاً"):
        asyncio.run(module.transcribe(wav(1)))
    with pytest.raises(ValueError):
        asyncio.run(module.decode_audio(b""))
    with pytest.raises(ValueError, match="10"):
        asyncio.run(module.decode_audio(wav(601)))


def test_native_transcription_errors_propagate(monkeypatch):
    async def broken(*args, **kwargs):
        raise RuntimeError("model_unavailable")

    monkeypatch.setattr(module.llm_engine, "create_chat_completion", broken)
    with pytest.raises(RuntimeError, match="model_unavailable"):
        asyncio.run(module.transcribe(wav(1)))
