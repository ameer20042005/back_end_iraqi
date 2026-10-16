# -*- coding: utf-8 -*-
"""فحص إقلاع خفيف: يبني التطبيق ومخطط OpenAPI بلا تشغيل lifespan أو موديلات."""

import asyncio

from app import main

app = main.app


def test_application_builds_its_openapi_schema():
    schema = app.openapi()

    assert schema["info"]["title"] == "Iraqi Backend API"
    assert "/voice_followup/calls" in schema["paths"]
    assert "/voice_followup/operator/chat/completions" in schema["paths"]
    assert "/v1/chat/completions" in schema["paths"]


def test_audio_warmup_runs_after_engine_is_ready(monkeypatch):
    calls = []

    class ReadyEngine:
        ready = True

    async def run(func):
        calls.append(func.__name__)

    def warmup_transcriber():
        pass

    monkeypatch.setattr(main, "llm_engine", ReadyEngine())
    monkeypatch.setattr(main, "run_in_threadpool", run)
    monkeypatch.setattr(main, "warmup_transcriber", warmup_transcriber)

    asyncio.run(main._warmup_audio_models())
    assert calls == ["warmup_transcriber"]
