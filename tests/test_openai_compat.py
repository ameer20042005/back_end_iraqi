# -*- coding: utf-8 -*-

import asyncio
import json

from app.features.openai_compat import router as compat
from app.features.support.prompts import SUPPORT_SYSTEM_PROMPT
from app.tool_loop import EXHAUSTED_FALLBACK


class _FakeEngine:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def create_chat_completion(self, messages, **kwargs):
        self.rendered = messages
        self.calls.append((messages, kwargs))
        return self.reply


def _request(messages, tools=None):
    return compat.ChatCompletionRequest(
        model="gemma-iraqi",
        messages=messages,
        tools=tools or [],
        tool_choice="auto",
    )


def _shipment_tool():
    return {
        "type": "function",
        "function": {
            "name": "searchShipments",
            "description": "Find a shipment",
            "parameters": {
                "type": "object",
                "properties": {"receiptNumber": {"type": "string"}},
            },
        },
    }


def test_tool_call_has_openai_shape_and_string_arguments(monkeypatch):
    engine = _FakeEngine({
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_native_1",
                    "type": "function",
                    "function": {
                        "name": "searchShipments",
                        "arguments": {"receiptNumber": "12345"},
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16},
    })
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request(
        [{"role": "user", "content": "وين وصلت شحنتي 12345؟"}],
        [_shipment_tool()],
    )))

    choice = response["choices"][0]
    tool_call = choice["message"]["tool_calls"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert tool_call["id"].startswith("call_")
    assert tool_call["function"]["name"] == "searchShipments"
    assert isinstance(tool_call["function"]["arguments"], str)
    assert json.loads(tool_call["function"]["arguments"]) == {"receiptNumber": "12345"}
    assert len(engine.calls) == 1
    assert engine.calls[0][1]["tools"][0]["function"]["name"] == "searchShipments"
    assert engine.calls[0][1]["tool_choice"] == "auto"
    assert engine.rendered[0]["role"] == "system"
    assert engine.rendered[0]["content"].startswith(SUPPORT_SYSTEM_PROMPT)
    assert response["usage"] == {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}


def test_tool_result_is_text_for_model_and_returns_final_answer(monkeypatch):
    engine = _FakeEngine({
        "choices": [{
            "message": {"role": "assistant", "content": "شحنتك حالياً قيد التوصيل ببغداد."},
            "finish_reason": "stop",
        }],
    })
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request([
        {"role": "user", "content": "وين وصلت شحنتي 12345؟"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "searchShipments",
                    "arguments": "{\"receiptNumber\":\"12345\"}",
                },
            }],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": "{\"stepName\":\"قيد التوصيل\",\"stateName\":\"بغداد\"}",
        },
    ], [_shipment_tool()])))

    choice = response["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert choice["message"] == {
        "role": "assistant",
        "content": "شحنتك حالياً قيد التوصيل ببغداد.",
    }
    assert any(message["role"] == "tool" for message in engine.rendered)
    result_message = next(
        message for message in engine.rendered
        if message["role"] == "tool"
    )
    assert "قيد التوصيل" in result_message["content"]


def test_general_message_returns_stop_without_tool_calls(monkeypatch):
    engine = _FakeEngine({
        "choices": [{
            "message": {"role": "assistant", "content": "هلا بيك، الحمد لله."},
            "finish_reason": "stop",
        }],
    })
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request([
        {"role": "user", "content": "شلونك"},
    ])))

    choice = response["choices"][0]
    assert choice["finish_reason"] == "stop"
    assert "tool_calls" not in choice["message"]


def test_malformed_model_output_returns_text_fallback(monkeypatch):
    engine = _FakeEngine({"unexpected": True})
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request([
        {"role": "user", "content": "وين طلبي؟"},
    ])))

    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["choices"][0]["message"]["content"] == EXHAUSTED_FALLBACK
