"""Sales is a thin OpenAI-compatible AI adapter with a sales persona."""

import asyncio
import json

import pytest
from fastapi import HTTPException

from app.config import settings
from app.features.sales.auth import require_sales_api_key
from app.features.sales import router as sales
from app.features.sales.prompts import SALES_OPENAI_SYSTEM_PROMPT, SALES_SYSTEM_PROMPT


class _FakeEngine:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def create_chat_completion(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return self.reply


def _request(messages, tools=None, **kwargs):
    return sales.SalesChatRequest(
        model="sales",
        messages=messages,
        tools=tools or [],
        tool_choice="auto",
        **kwargs,
    )


def _search_tool():
    return {
        "type": "function",
        "function": {
            "name": "search_products",
            "description": "Search products",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
        },
    }


def test_sales_runs_one_generation_with_client_messages(monkeypatch):
    engine = _FakeEngine({"choices": [{"message": {"content": "هلا بيك"}}]})
    monkeypatch.setattr(sales, "llm_engine", engine)
    response = asyncio.run(sales.sales_chat(_request([
        {"role": "user", "content": "هلا"},
    ]), "key"))

    assert len(engine.calls) == 1
    assert engine.calls[0][0][0] == {"role": "system", "content": SALES_OPENAI_SYSTEM_PROMPT}
    assert engine.calls[0][0][-1] == {"role": "user", "content": "هلا"}
    assert response["choices"][0]["message"]["content"] == "هلا بيك"


def test_original_sales_system_prompt_is_preserved():
    assert SALES_OPENAI_SYSTEM_PROMPT.startswith(SALES_SYSTEM_PROMPT)


def test_native_tool_call_is_forwarded_without_execution(monkeypatch):
    engine = _FakeEngine({
        "choices": [{"message": {"tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "search_products", "arguments": {"query": "غسالة"}},
        }]}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
    })
    monkeypatch.setattr(sales, "llm_engine", engine)
    response = asyncio.run(sales.sales_chat(_request(
        [{"role": "user", "content": "عندكم غسالات؟"}], [_search_tool()]
    ), "key"))

    call = response["choices"][0]["message"]["tool_calls"][0]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert call["function"]["name"] == "search_products"
    assert json.loads(call["function"]["arguments"]) == {"query": "غسالة"}
    assert engine.calls[0][1]["tools"][0]["function"]["name"] == "search_products"
    assert response["usage"]["total_tokens"] == 12


def test_client_tool_result_is_preserved_for_next_generation(monkeypatch):
    engine = _FakeEngine({"choices": [{"message": {"content": "سعرها 500,000 دينار."}}]})
    monkeypatch.setattr(sales, "llm_engine", engine)
    history = [
        {"role": "user", "content": "عندكم غسالة؟"},
        {"role": "assistant", "tool_calls": [{
            "id": "call_1", "type": "function",
            "function": {"name": "search_products", "arguments": "{\"query\":\"غسالة\"}"},
        }]},
        {"role": "tool", "tool_call_id": "call_1", "content": "{\"price\":500000}"},
    ]
    response = asyncio.run(sales.sales_chat(_request(history, [_search_tool()]), "key"))

    assert engine.calls[0][0][-1]["role"] == "tool"
    assert engine.calls[0][0][-1]["tool_call_id"] == "call_1"
    assert response["choices"][0]["message"]["content"] == "سعرها 500,000 دينار."


def test_unknown_or_malformed_tool_call_returns_fallback(monkeypatch):
    engine = _FakeEngine({"choices": [{"message": {
        "content": None,
        "tool_calls": [{"function": {"name": "unknown", "arguments": "broken"}}],
    }}]})
    monkeypatch.setattr(sales, "llm_engine", engine)
    response = asyncio.run(sales.sales_chat(_request(
        [{"role": "user", "content": "دور"}], [_search_tool()]
    ), "key"))
    assert response["choices"][0]["finish_reason"] == "stop"
    assert response["choices"][0]["message"]["content"]


def test_multimodal_message_is_forwarded_unchanged(monkeypatch):
    parts = [
        {"type": "text", "text": "هذا موجود؟"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    engine = _FakeEngine({"choices": [{"message": {"content": "أتأكدلك"}}]})
    monkeypatch.setattr(sales, "llm_engine", engine)
    asyncio.run(sales.sales_chat(_request([{"role": "user", "content": parts}]), "key"))
    assert engine.calls[0][0][-1]["content"] == parts


def test_stream_name_returns_openai_sse(monkeypatch):
    engine = _FakeEngine({"choices": [{"message": {"content": "هلا بيك"}}]})
    monkeypatch.setattr(sales, "llm_engine", engine)
    response = asyncio.run(sales.sales_chat_stream(_request([
        {"role": "user", "content": "هلا"},
    ]), "key"))

    async def collect():
        return [chunk async for chunk in response.body_iterator]

    events = asyncio.run(collect())
    assert json.loads(events[0].removeprefix("data: "))["object"] == "chat.completion.chunk"
    assert events[-1] == "data: [DONE]\n\n"


def test_public_function_names_are_kept():
    assert sales.sales_chat.__name__ == "sales_chat"
    assert sales.sales_chat_stream.__name__ == "sales_chat_stream"


def test_sales_api_key_is_owned_by_feature():
    assert asyncio.run(require_sales_api_key(settings.sales_api_key)) == settings.sales_api_key
    with pytest.raises(HTTPException) as error:
        asyncio.run(require_sales_api_key("wrong"))
    assert error.value.status_code == 401
