import asyncio
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from app.features.voice_followup import stt


@pytest.fixture
def whisper(monkeypatch):
    loads, generations = [], []
    active = {"count": 0, "peak": 0}

    class Processor:
        tokenizer = SimpleNamespace(all_special_ids=[1, 2])
        @classmethod
        def from_pretrained(cls, repository):
            loads.append(("processor", repository))
            return cls()

        def __call__(self, samples, **kwargs):
            assert samples.dtype == np.float32
            assert kwargs == {"sampling_rate": 16000, "return_tensors": "pt", "return_attention_mask": True}
            return SimpleNamespace(input_features=torch.ones(1, 80, 3000), attention_mask=torch.ones(1, 3000))

        def batch_decode(self, tokens, **kwargs):
            assert kwargs == {"skip_special_tokens": True}
            assert tokens == [[7]]
            return [" اي وصل الطلب "]

    class Model:
        dtype = torch.float32
        generation_config = SimpleNamespace(forced_decoder_ids=[1], eos_token_id=2)

        @classmethod
        def from_pretrained(cls, repository, **kwargs):
            loads.append(("model", repository))
            assert kwargs == {"dtype": torch.float32, "attn_implementation": "sdpa"}
            return cls()

        def to(self, device):
            assert device == "cpu"
            return self

        def eval(self):
            return self

        def generate(self, features, **kwargs):
            assert not torch.is_grad_enabled()
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])
            time.sleep(0.01)
            active["count"] -= 1
            generations.append(kwargs)
            return SimpleNamespace(sequences=torch.tensor([[1, 7, 2]]))

    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        WhisperProcessor=Processor, WhisperForConditionalGeneration=Model))
    for name in ("_model", "_processor", "_device", "_last_transcription"):
        monkeypatch.setattr(stt, name, None)
    from dataclasses import replace
    monkeypatch.setattr(stt, "settings", replace(stt.settings, stt_device="cpu"))
    return loads, generations, active, Model


def test_whisper_loads_once_serializes_inference_and_uses_arabic(whisper):
    loads, generations, active, model = whisper

    async def check():
        return await asyncio.gather(*(stt.transcribe(np.ones(16000, dtype=np.float32)) for _ in range(3)))

    assert asyncio.run(check()) == ["اي وصل الطلب"] * 3
    assert loads == [("processor", "ayoubkirouane/whisper-small-ar"), ("model", "ayoubkirouane/whisper-small-ar")]
    assert active["peak"] == 1
    assert model.generation_config.forced_decoder_ids is None
    assert all(g["language"] == "ar" and g["task"] == "transcribe" and g["num_beams"] == 1
               and g["do_sample"] is False for g in generations)


@pytest.mark.parametrize("samples, rate", [
    (np.ones(10), 8000), (np.array([]), 16000), (np.ones((2, 10)), 16000),
    (np.array([np.nan]), 16000), (np.ones(16000 * 31), 16000),
])
def test_invalid_audio_never_loads_weights(whisper, samples, rate):
    with pytest.raises(ValueError):
        stt.transcribe_array(samples, rate)
    assert whisper[0] == []


def test_truncated_asr_is_not_recorded_as_a_complete_answer(whisper, monkeypatch):
    monkeypatch.setattr(whisper[3], "generate", lambda *args, **kwargs: SimpleNamespace(sequences=torch.tensor([[1, 7, 8]])))
    with pytest.raises(ValueError, match="كاملاً"):
        stt.transcribe_array(np.ones(16000, dtype=np.float32))
    assert stt._last_transcription["ok"] is False


def test_health_inspection_never_loads_whisper(monkeypatch):
    monkeypatch.setattr(stt, "_model", None)
    monkeypatch.setattr(stt, "_load", lambda: pytest.fail("Health inspection loaded weights"))
    assert stt.status()["loaded"] is False
