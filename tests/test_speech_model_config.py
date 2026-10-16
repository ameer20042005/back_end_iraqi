# -*- coding: utf-8 -*-
"""اختبارات مصدر إعدادات موديلات الصوت الموحّد."""

import sys
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings


def test_speech_models_come_only_from_settings():
    settings = Settings()

    assert settings.stt_model().repository == settings.whisper_model_ar
    assert settings.tts_model().repository == settings.tts_model_ar


def test_environment_variables_do_not_override_static_settings(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "example/legacy-stt")
    monkeypatch.setenv("TTS_MODEL", "example/legacy-tts")

    settings = Settings()

    assert settings.stt_model().repository == "ayoubkirouane/whisper-small-ar"
    assert settings.tts_model().repository == "ResembleAI/chatterbox"


def test_transcription_reuses_arabic_pipeline_and_generation_settings(monkeypatch):
    from app.features.order_intake import transcribe as module

    loaded = []
    calls = []

    def pipe(audio, **kwargs):
        calls.append((audio, kwargs))
        return {"text": "  هلا بيك  "}

    def factory(task, **kwargs):
        loaded.append((task, kwargs))
        return pipe

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setattr(module, "_TRANSFORMERS_AVAILABLE", True)
    monkeypatch.setattr(module, "pipeline", factory, raising=False)
    monkeypatch.setattr(module, "_pipeline", None)
    assert module.transcribe(b"first") == "هلا بيك"
    assert module.transcribe(b"second") == "هلا بيك"
    assert len(loaded) == 1
    assert loaded[0][0] == "automatic-speech-recognition"
    assert loaded[0][1]["model"] == module.settings.whisper_model_ar
    assert loaded[0][1]["chunk_length_s"] == 30
    assert loaded[0][1]["stride_length_s"] == (3, 2)
    assert all(kwargs["generate_kwargs"] == {"task": "transcribe", "language": "arabic"}
               for _, kwargs in calls)


def test_speech_uses_one_model_and_local_reference(monkeypatch):
    from app.features.voice_followup import tts

    loaded = []
    instance = SimpleNamespace()

    def from_pretrained(**kwargs):
        loaded.append(kwargs)
        return instance

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setattr(tts, "ChatterboxMultilingualTTS",
                        SimpleNamespace(from_pretrained=from_pretrained), raising=False)
    monkeypatch.setattr(tts, "_instance", None)
    first = tts._get_instance()
    assert tts._get_instance() is first
    assert loaded == [{"device": "cpu", "t3_model": tts.settings.tts_ckpt_ar}]
    assert first[0] is instance
    assert first[1] == str(Path(tts.__file__).resolve().parent / tts.settings.tts_ref_audio_ar)
    assert Path(first[1]).is_file()
    assert first[2] == tts.settings.tts_language_ar == "ar"


def test_speech_warmup_reports_failed_synthesis(monkeypatch):
    from app.features.voice_followup import tts

    monkeypatch.setattr(tts, "_CHATTERBOX_AVAILABLE", True)
    monkeypatch.setattr(tts, "synthesize", lambda text: None)
    assert tts.warmup() is False
