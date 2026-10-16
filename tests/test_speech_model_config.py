# -*- coding: utf-8 -*-
"""اختبارات مصدر إعدادات موديلات الصوت الموحّد."""

import sys
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
