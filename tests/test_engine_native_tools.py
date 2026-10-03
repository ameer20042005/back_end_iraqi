# -*- coding: utf-8 -*-

import asyncio

from app.engine import LLMEngine


def test_native_completion_passes_tools_without_guided_json(monkeypatch):
    engine = LLMEngine()
    captured = {}

    async def fake_chat_completion(messages, **kwargs):
        captured["messages"] = messages
        captured.update(kwargs)
        return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(engine, "_chat_completion", fake_chat_completion)
    tools = [{
        "type": "function",
        "function": {
            "name": "searchShipments",
            "parameters": {"type": "object", "properties": {}},
        },
    }]

    asyncio.run(engine.create_chat_completion(
        [{"role": "user", "content": "وين الشحنة؟"}],
        tools=tools,
        tool_choice="auto",
    ))

    assert captured["tools"] == tools
    assert captured["tool_choice"] == "auto"
    assert captured["guided_json"] is None


def test_chat_completion_body_disables_reasoning_for_all_request_shapes():
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {}}}]
    bodies = [
        LLMEngine._build_body([{"role": "user", "content": "hi"}], 32, None, None),
        LLMEngine._build_body([{"role": "user", "content": "hi"}], 32, None,
                              {"type": "object"}, stream=True),
        LLMEngine._build_body([{"role": "user", "content": "hi"}], 32, None, None,
                              tools=tools, tool_choice="auto"),
    ]
    for body in bodies:
        assert body["reasoning_effort"] == "none"
        assert body["chat_template_kwargs"] == {"enable_thinking": False}


def test_vllm_rejection_surfaces_upstream_message():
    import httpx
    import pytest

    from app.engine import LLMUpstreamError

    message = "default chat template is no longer allowed"

    def handler(request):
        return httpx.Response(400, json={"error": {"message": message, "code": 400}})

    engine = LLMEngine()
    engine._ready = True
    engine._client = httpx.AsyncClient(
        base_url="http://vllm.test/v1", transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMUpstreamError) as info:
        asyncio.run(engine._chat_completion(
            [{"role": "user", "content": "مرحبا"}], 16, None, None,
        ))

    assert info.value.message == message
    assert info.value.upstream_status == 400
    assert engine.metrics["errors"] == 1
