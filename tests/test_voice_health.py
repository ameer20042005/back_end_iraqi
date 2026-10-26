# -*- coding: utf-8 -*-
"""صحة موديلات صوت المكالمات بلا GPU ولا موديلات حقيقية.

  ١. تصنيف أسباب فشل النطق (cuda_oom / not_installed / load_failed / generation_failed).
  ٢. تحميل F5-TTS العراقي (أوزان + مفردات) ونقل أخطاء التحميل/التوليد.
  ٣. GET /health/voice: الحالة بلا تحميل، ونتيجة آخر محاولة نطق.
  ٤. VOICE_STREAM_WARMUP: الافتراضي على GPU بس، والتحميل المسبق يسجّل الفشل.
"""

import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
import torch
from fastapi.testclient import TestClient

from app import config, main
from app.features.voice_followup import tts, stt, voice_health


@pytest.fixture(autouse=True)
def native_model_ready(monkeypatch):
    monkeypatch.setattr(main.llm_engine, "_ready", True)
    monkeypatch.setattr(stt, "status", lambda: {"engine": "whisper", "model": config.settings.stt_model,
                                               "installed": True, "loaded": False, "last_transcription": None})
    monkeypatch.setattr(stt, "warmup", lambda: True)


# ── ١. تصنيف الأسباب ───────────────────────────────────────────────────────

def test_classify_error_reasons():
    oom = torch.cuda.OutOfMemoryError("CUDA out of memory. Tried to allocate 2.00 GiB")
    assert voice_health.classify_error(oom) == "cuda_oom"
    assert voice_health.classify_error(RuntimeError("CUDA error: out of memory")) == "cuda_oom"
    assert voice_health.classify_error(voice_health.NotInstalledError("x")) == "not_installed"
    assert voice_health.classify_error(ModuleNotFoundError("No module named 'perth'")) == "not_installed"
    assert voice_health.classify_error(RuntimeError("shape mismatch")) == "generation_failed"
    assert voice_health.classify_error(None) == "generation_failed"


def test_classify_error_looks_through_the_load_wrapper():
    def wrapped(cause):
        try:
            raise voice_health.ModelLoadError("تعذّر تحميل F5-TTS") from cause
        except voice_health.ModelLoadError as exc:
            return exc

    assert voice_health.classify_error(wrapped(TypeError("unexpected keyword argument 't3_model'"))) == "load_failed"
    # نفاد الذاكرة أثناء التحميل يبقى cuda_oom مو load_failed.
    assert voice_health.classify_error(wrapped(torch.cuda.OutOfMemoryError("CUDA out of memory"))) == "cuda_oom"
    assert voice_health.classify_error(wrapped(ImportError("perth"))) == "not_installed"


def test_describe_failure_is_one_line_without_cuda():
    line = voice_health.describe_failure(RuntimeError("boom"))
    assert "\n" not in line
    assert line == "reason=generation_failed error=RuntimeError: boom vram_free_gb=n/a"


# ── ٢. تحميل F5-TTS ─────────────────────────────────────────────────────────

class StrictVoice:
    """مثل f5_tts.api.F5TTS: أوزان ومفردات بالمُنشئ، وinfer() يرجع (wav, sr, spec)."""

    loads = []

    def __init__(self, ckpt_file, vocab_file):
        type(self).loads.append((ckpt_file, vocab_file))

    def infer(self, **kwargs):
        return torch.zeros(10).numpy(), 24000, None


class BrokenGenerateVoice(StrictVoice):
    def infer(self, **kwargs):
        raise torch.cuda.OutOfMemoryError("CUDA out of memory. Tried to allocate 64.00 MiB")


class OomLoadVoice(StrictVoice):
    def __init__(self, ckpt_file, vocab_file):
        raise torch.cuda.OutOfMemoryError("CUDA out of memory while loading")


@pytest.fixture
def fake_tts(monkeypatch):
    def install(voice_cls):
        voice_cls.loads = []
        monkeypatch.setitem(sys.modules, "huggingface_hub",
                            SimpleNamespace(hf_hub_download=lambda repo_id, filename: filename))
        monkeypatch.setattr(tts, "F5TTS", voice_cls, raising=False)
        monkeypatch.setattr(tts, "_F5_TTS_AVAILABLE", True)
        monkeypatch.setattr(tts, "_IMPORT_ERROR", None)
        monkeypatch.setattr(tts, "settings", replace(tts.settings, tts_pool_size=1))
        monkeypatch.setattr(tts, "_pool", None)
        monkeypatch.setattr(tts, "_last_synthesis", None)
        return tts

    return install


def test_loads_iraqi_checkpoint_and_vocabulary(fake_tts):
    module = fake_tts(StrictVoice)
    audio, sr = module.synthesize_array("هلا بيك")
    assert sr == 24000 and len(audio) == 10
    assert StrictVoice.loads == [(tts.settings.tts_ckpt_ar, tts.settings.tts_vocab_ar)]


def test_last_synthesis_records_success_and_failure(fake_tts):
    module = fake_tts(StrictVoice)
    module.synthesize_array("هلا")
    last = module.status()["last_synthesis"]
    assert last["ok"] is True and last["reason"] is None and last["error_type"] is None

    module = fake_tts(BrokenGenerateVoice)
    with pytest.raises(torch.cuda.OutOfMemoryError):
        module.synthesize_array("هلا")
    last = module.status()["last_synthesis"]
    assert last == {**last, "ok": False, "reason": "cuda_oom", "error_type": "OutOfMemoryError"}


def test_load_failure_is_wrapped_and_the_slot_is_freed(fake_tts):
    module = fake_tts(OomLoadVoice)
    with pytest.raises(voice_health.ModelLoadError):
        module.synthesize_array("هلا")
    assert module.status()["last_synthesis"]["reason"] == "cuda_oom"
    assert module.status()["instances"] == 0
    assert module._get_pool()._created == 0  # المحاولة الجاية تعيد التحميل


# ── ٣. GET /health/voice ───────────────────────────────────────────────────

def _no_load(*args, **kwargs):
    raise AssertionError("الفحص حمّل موديل!")


def test_health_voice_reports_without_loading_models(fake_tts, monkeypatch):
    fake_tts(StrictVoice)
    monkeypatch.setattr(StrictVoice, "__init__", _no_load)

    response = TestClient(main.app).get("/health/voice")  # بلا مفتاح
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "healthy"
    assert body["tts"] == {"installed": True, "import_error": None, "loaded": False, "instances": 0,
                           "pool_size": 1, "last_synthesis": None}
    assert body["gpu"] == {"cuda": False}
    assert body["warmup"]["enabled"] is False and body["warmup"]["state"] == "disabled"


def test_health_voice_shows_loaded_and_last_failure(fake_tts, monkeypatch):
    module = fake_tts(StrictVoice)
    module.synthesize_array("هلا")
    body = TestClient(main.app).get("/health/voice").json()
    assert body["tts"]["loaded"] is True and body["tts"]["instances"] == 1
    assert body["tts"]["last_synthesis"]["ok"] is True

    # آخر نطق فشل ← 503 والسبب ظاهر بلا رسالة الخطأ نفسها.
    monkeypatch.setattr(StrictVoice, "infer", BrokenGenerateVoice.infer)
    with pytest.raises(torch.cuda.OutOfMemoryError):
        module.synthesize_array("هلا")
    response = TestClient(main.app).get("/health/voice")
    assert response.status_code == 503
    last = response.json()["tts"]["last_synthesis"]
    assert last["reason"] == "cuda_oom" and "message" not in last


def test_health_voice_unhealthy_when_f5_tts_missing(monkeypatch):
    monkeypatch.setattr(tts, "_F5_TTS_AVAILABLE", False)
    monkeypatch.setattr(tts, "_IMPORT_ERROR", "OSError")
    monkeypatch.setattr(tts, "_pool", None)
    monkeypatch.setattr(tts, "_last_synthesis", None)
    response = TestClient(main.app).get("/health/voice")
    assert response.status_code == 503
    assert response.json()["tts"]["installed"] is False
    assert response.json()["tts"]["import_error"] == "OSError"


# ── ٤. التحميل المسبق ──────────────────────────────────────────────────────

def test_env_flag_parsing(monkeypatch):
    for raw, expected in [("1", True), ("true", True), ("0", False), ("no", False), ("", None)]:
        monkeypatch.setenv("VOICE_STREAM_WARMUP", raw)
        assert config.Settings().voice_stream_warmup is expected
    monkeypatch.delenv("VOICE_STREAM_WARMUP")
    assert config.Settings().voice_stream_warmup is None


@pytest.mark.parametrize("flag,cuda,expected", [
    (None, True, True), (None, False, False), (True, False, True), (False, True, False),
])
def test_warmup_default_is_gpu_only(monkeypatch, flag, cuda, expected):
    monkeypatch.setattr(voice_health, "settings", replace(voice_health.settings, voice_stream_warmup=flag))
    monkeypatch.setattr(voice_health, "_cuda_available", lambda: cuda)
    assert voice_health.warmup_enabled() is expected


@pytest.fixture
def warm_state(monkeypatch):
    monkeypatch.setattr(voice_health, "_warmup", {"state": "pending", "tts": None, "seconds": None})
    monkeypatch.setattr(voice_health, "settings", replace(voice_health.settings, voice_stream_warmup=True))


def test_warmup_loads_both_models(warm_state, monkeypatch):
    calls = []
    monkeypatch.setattr(tts, "warmup", lambda: calls.append("tts") or True)
    monkeypatch.setattr(stt, "warmup", lambda: calls.append("stt") or True)
    result = voice_health.warmup_voice_models()
    assert calls == ["stt", "tts"]
    assert result["state"] == "done" and result["tts"] is True
    assert voice_health.warmup_state()["enabled"] is True


def test_warmup_failure_is_logged_and_marks_health_unhealthy(warm_state, monkeypatch, caplog):
    monkeypatch.setattr(tts, "warmup", lambda: False)
    with caplog.at_level("ERROR", logger=voice_health.__name__):
        result = voice_health.warmup_voice_models()
    assert result["state"] == "failed" and result["tts"] is False
    assert any("tts=False" in r.getMessage() for r in caplog.records)
    response = TestClient(main.app).get("/health/voice")
    assert response.status_code == 503 and response.json()["warmup"]["state"] == "failed"


def test_real_warmup_with_strict_library_succeeds(warm_state, fake_tts, monkeypatch):
    """التحميل المسبق الحقيقي (tts.warmup) مع موديل وهمي ينجح."""
    module = fake_tts(StrictVoice)
    assert voice_health.warmup_voice_models()["state"] == "done"
    assert module.status()["loaded"] is True


def test_lifespan_runs_voice_warmup_when_enabled(monkeypatch):
    ran = []
    monkeypatch.setattr(main.llm_engine, "_ready", True)
    monkeypatch.setattr(voice_health, "warmup_enabled", lambda: True)
    monkeypatch.setattr(voice_health, "warmup_voice_models", lambda: ran.append(True))
    import asyncio

    asyncio.run(main._warmup_audio_models())
    assert ran == [True]

    ran.clear()
    monkeypatch.setattr(voice_health, "warmup_enabled", lambda: False)
    asyncio.run(main._warmup_audio_models())
    assert ran == []


# ── ميزانية الذاكرة ────────────────────────────────────────────────────────

def test_gpu_memory_utilization_env_override(monkeypatch):
    monkeypatch.delenv("GPU_MEMORY_UTILIZATION", raising=False)
    assert config.Settings().gpu_memory_utilization == 0.70
    monkeypatch.setenv("GPU_MEMORY_UTILIZATION", "0.8")
    assert config.Settings().gpu_memory_utilization == 0.8
    monkeypatch.setenv("GPU_MEMORY_UTILIZATION", "0.99")
    with pytest.raises(ValueError):
        config.Settings()


def test_gpu_diagnostics_separate_process_cache_from_whole_device(monkeypatch):
    gb = 1024**3
    cuda = SimpleNamespace(is_available=lambda: True, mem_get_info=lambda device: (2 * gb, 8 * gb),
                           memory_allocated=lambda device: gb, memory_reserved=lambda device: 3 * gb)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=cuda))
    snapshot = voice_health.gpu_memory()
    assert snapshot["vram_free_gb"] == 2 and snapshot["vram_total_gb"] == 8
    assert snapshot["process_allocated_gb"] == 1 and snapshot["process_reserved_gb"] == 3
    assert snapshot["process_cached_unused_gb"] == 2
