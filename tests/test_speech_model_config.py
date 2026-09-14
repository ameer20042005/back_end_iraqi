# -*- coding: utf-8 -*-
"""اختبارات مصدر إعدادات موديلات الصوت الموحّد."""

from app.config import Settings
from app.lang import Lang


def test_language_model_selection_comes_only_from_settings():
    settings = Settings()

    assert settings.stt_model_for(Lang.AR).repository == settings.whisper_model_ar
    assert settings.stt_model_for(Lang.KU).repository == settings.whisper_model_ku
    assert settings.tts_model_for(Lang.AR).repository == settings.tts_model_ar
    assert settings.tts_model_for(Lang.KU).repository == settings.tts_model_ku


def test_legacy_arabic_model_environment_names_remain_supported(monkeypatch):
    monkeypatch.setenv("WHISPER_MODEL", "example/legacy-stt")
    monkeypatch.setenv("TTS_MODEL", "example/legacy-tts")

    settings = Settings()

    assert settings.stt_model_for(Lang.AR).repository == "example/legacy-stt"
    assert settings.tts_model_for(Lang.AR).repository == "example/legacy-tts"
