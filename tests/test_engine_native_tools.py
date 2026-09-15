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

