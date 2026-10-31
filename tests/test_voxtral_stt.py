import asyncio
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from app.features.voice_followup import stt, voice_health


@pytest.fixture
def voxtral(monkeypatch):
    loads, generations = [], []
    active = {"count": 0, "peak": 0}

    class Inputs(dict):
        def to(self, device, dtype):
            assert device == "cpu" and dtype == torch.bfloat16
            return self

    class Processor:
        @classmethod
        def from_pretrained(cls, repository):
            loads.append(("processor", repository))
            return cls()

        def __call__(self, samples, **kwargs):
            assert samples.dtype == np.float32
            assert kwargs == {"return_tensors": "pt"}
            return Inputs(input_ids=torch.ones(1, 39, dtype=torch.long), input_features=torch.ones(1, 128, 3000),
                          num_delay_tokens=torch.tensor(6))

        def batch_decode(self, tokens, **kwargs):
            assert kwargs == {"skip_special_tokens": True}
            assert tokens.tolist() == [[1, 7, 2]]
            return [" اي وصل الطلب "]

    class Model:
        dtype = torch.bfloat16

        @classmethod
        def from_pretrained(cls, repository, **kwargs):
            loads.append(("model", repository))
            assert kwargs == {"dtype": torch.bfloat16}
            return cls()

        def to(self, device):
            assert device == "cpu"
            return self

        def eval(self):
            return self

        def generate(self, **kwargs):
            assert not torch.is_grad_enabled()
            active["count"] += 1
            active["peak"] = max(active["peak"], active["count"])
            time.sleep(0.01)
            active["count"] -= 1
            generations.append(kwargs)
            return torch.tensor([[1, 7, 2]])

    monkeypatch.setitem(sys.modules, "mistral_common", SimpleNamespace())
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(
        AutoProcessor=Processor, VoxtralRealtimeForConditionalGeneration=Model))
    for name in ("_model", "_processor", "_device", "_last_transcription"):
        monkeypatch.setattr(stt, name, None)
    from dataclasses import replace
    monkeypatch.setattr(stt, "settings", replace(stt.settings, stt_device="cpu"))
    return loads, generations, active, Model


def test_voxtral_loads_once_serializes_inference_and_passes_the_processor_inputs(voxtral):
    loads, generations, active, _ = voxtral

    async def check():
        return await asyncio.gather(*(stt.transcribe(np.ones(16000, dtype=np.float32)) for _ in range(3)))

    repository = "mistralai/Voxtral-Mini-4B-Realtime-Arabic"
    assert asyncio.run(check()) == ["اي وصل الطلب"] * 3
    assert loads == [("processor", repository), ("model", repository)]
    assert active["peak"] == 1
    assert all(g["do_sample"] is False and g["num_delay_tokens"] == 6 and "input_features" in g
               for g in generations)
    assert stt._last_transcription["ok"] is True


@pytest.mark.parametrize("samples, rate", [
    (np.ones(10), 8000), (np.array([]), 16000), (np.ones((2, 10)), 16000),
    (np.array([np.nan]), 16000), (np.ones(16000 * 31), 16000),
])
def test_invalid_audio_never_loads_weights(voxtral, samples, rate):
    with pytest.raises(ValueError):
        stt.transcribe_array(samples, rate)
    assert voxtral[0] == []


def test_silence_never_loads_weights(voxtral):
    assert stt.transcribe_array(np.zeros(16000, dtype=np.float32)) == ""
    assert voxtral[0] == []


def test_generation_failure_is_recorded_and_raised(voxtral, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("generation failed")

    monkeypatch.setattr(voxtral[3], "generate", broken)
    with pytest.raises(RuntimeError, match="generation failed"):
        stt.transcribe_array(np.ones(16000, dtype=np.float32))
    assert stt._last_transcription == {"ok": False, "error_type": "RuntimeError",
                                       "elapsed_ms": stt._last_transcription["elapsed_ms"]}


def test_missing_mistral_common_is_reported_as_not_installed(voxtral, monkeypatch):
    monkeypatch.setitem(sys.modules, "mistral_common", None)
    with pytest.raises(voice_health.NotInstalledError) as error:
        stt.transcribe_array(np.ones(16000, dtype=np.float32))
    assert voice_health.classify_error(error.value) == voice_health.NOT_INSTALLED
    assert voxtral[0] == []


def test_health_inspection_never_loads_voxtral(monkeypatch):
    monkeypatch.setattr(stt, "_model", None)
    monkeypatch.setattr(stt, "_load", lambda: pytest.fail("Health inspection loaded weights"))
    status = stt.status()
    assert status["engine"] == "voxtral" and status["loaded"] is False
