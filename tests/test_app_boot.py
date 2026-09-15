# -*- coding: utf-8 -*-
"""فحص إقلاع خفيف: يبني التطبيق ومخطط OpenAPI بلا تشغيل lifespan أو موديلات."""

from app.main import app


def test_application_builds_its_openapi_schema():
    schema = app.openapi()

    assert schema["info"]["title"] == "Iraqi Backend API"
    assert "/voice_followup/postpone/respond" in schema["paths"]
    assert "/support/chat" in schema["paths"]
    assert "/v1/chat/completions" in schema["paths"]
