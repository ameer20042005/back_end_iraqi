# -*- coding: utf-8 -*-

import asyncio
import json

from app.features.openai_compat import router as compat
from app.features.openai_compat.prompts import OPENAI_COMPAT_SYSTEM_PROMPT
from app.fallback import EXHAUSTED_FALLBACK


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
    assert engine.rendered[0]["content"].startswith(OPENAI_COMPAT_SYSTEM_PROMPT)
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


# ---------------------------------------------------------------------------
# Vision, model availability and argument validation
# ---------------------------------------------------------------------------

import pytest

from app import engine as engine_module
from app import model_status
from app.engine import LLMUpstreamError

_PNG_URI = "data:image/png;base64,iVBORw0KGgo="


def _status(**overrides):
    values = dict(model="m", reachable=True, loaded=True, vision=True, backend="lmstudio")
    values.update(overrides)
    return model_status.ModelStatus(**values)


@pytest.fixture(autouse=True)
def _healthy_model(monkeypatch):
    """Every test in this module starts with a reachable, loaded vision model."""
    async def healthy(force=False):
        return _status()
    monkeypatch.setattr(compat, "get_model_status", healthy)


def _use_status(monkeypatch, status):
    async def fixed(force=False):
        return status
    monkeypatch.setattr(compat, "get_model_status", fixed)


def _image_message():
    return {"role": "user", "content": [
        {"type": "text", "text": "What is in the image?"},
        {"type": "image_url", "image_url": {"url": _PNG_URI}},
    ]}


def test_image_parts_reach_the_model_unflattened(monkeypatch):
    engine = _FakeEngine({"choices": [{"message": {"role": "assistant", "content": "A red circle."}}]})
    monkeypatch.setattr(compat, "llm_engine", engine)

    asyncio.run(compat.chat_completions(_request([_image_message()])))

    user = engine.rendered[1]
    assert user["content"] == [
        {"type": "text", "text": "What is in the image?"},
        {"type": "image_url", "image_url": {"url": _PNG_URI}},
    ]


def test_image_is_rejected_when_the_model_has_no_vision(monkeypatch):
    engine = _FakeEngine({})
    monkeypatch.setattr(compat, "llm_engine", engine)
    _use_status(monkeypatch, _status(vision=False))

    with pytest.raises(LLMUpstreamError) as info:
        asyncio.run(compat.chat_completions(_request([_image_message()])))

    assert info.value.code == "MODEL_DOES_NOT_SUPPORT_VISION"
    assert engine.calls == []  # the image never left the service


def test_text_only_request_ignores_missing_vision(monkeypatch):
    engine = _FakeEngine({"choices": [{"message": {"role": "assistant", "content": "ok"}}]})
    monkeypatch.setattr(compat, "llm_engine", engine)
    _use_status(monkeypatch, _status(vision=False))

    response = asyncio.run(compat.chat_completions(_request([{"role": "user", "content": "hi"}])))

    assert response["choices"][0]["message"]["content"] == "ok"


@pytest.mark.parametrize("status, code", [
    (dict(reachable=False, loaded=None, backend="unreachable"), "LM_STUDIO_UNAVAILABLE"),
    (dict(loaded=False), "AI_MODEL_NOT_LOADED"),
])
def test_unavailable_model_server_is_reported_with_a_code(monkeypatch, status, code):
    engine = _FakeEngine({})
    monkeypatch.setattr(compat, "llm_engine", engine)
    _use_status(monkeypatch, _status(**status))

    with pytest.raises(LLMUpstreamError) as info:
        asyncio.run(compat.chat_completions(_request([{"role": "user", "content": "hi"}])))

    assert info.value.code == code
    assert engine.calls == []


@pytest.mark.parametrize("arguments", ["[1, 2]", "\"12345\"", "{not json"])
def test_tool_call_with_non_object_arguments_is_not_forwarded(monkeypatch, arguments):
    engine = _FakeEngine({"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "searchShipments", "arguments": arguments}}],
    }}]})
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request(
        [{"role": "user", "content": "وين شحنتي؟"}], [_shipment_tool()])))

    assert "tool_calls" not in response["choices"][0]["message"]


def test_unknown_tool_name_is_not_forwarded(monkeypatch):
    engine = _FakeEngine({"choices": [{"message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c1", "type": "function",
                        "function": {"name": "deleteAllShipments", "arguments": "{}"}}],
    }}]})
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request(
        [{"role": "user", "content": "احذف كلشي"}], [_shipment_tool()])))

    assert "tool_calls" not in response["choices"][0]["message"]


def test_engine_maps_timeouts_and_connection_errors_to_codes():
    import httpx

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request):
        raise httpx.ConnectError("refused", request=request)

    for handler, code in ((timeout, "AI_REQUEST_TIMEOUT"), (refused, "LM_STUDIO_UNAVAILABLE")):
        engine = engine_module.LLMEngine()
        engine._ready = True
        engine._client = httpx.AsyncClient(base_url="http://llm.test/v1",
                                           transport=httpx.MockTransport(handler))
        with pytest.raises(LLMUpstreamError) as info:
            asyncio.run(engine._chat_completion([{"role": "user", "content": "x"}], 8, None, None))
        assert info.value.code == code


def test_engine_reports_not_ready_server_as_unavailable():
    engine = engine_module.LLMEngine()
    with pytest.raises(LLMUpstreamError) as info:
        asyncio.run(engine._chat_completion([{"role": "user", "content": "x"}], 8, None, None))
    assert info.value.code == "LM_STUDIO_UNAVAILABLE"


def test_model_status_reads_lmstudio_load_state_and_vision():
    import httpx

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m"}, {"id": "other"}]})
        return httpx.Response(200, json={"data": [
            {"id": "m", "type": "vlm", "state": "not-loaded"},
            {"id": "other", "type": "llm", "state": "loaded"},
        ]})

    status = asyncio.run(model_status._probe("m", transport=httpx.MockTransport(handler)))
    assert status.reachable and status.loaded is False and status.vision is True

    status = asyncio.run(model_status._probe("other", transport=httpx.MockTransport(handler)))
    assert status.loaded is True and status.vision is False


def test_model_status_falls_back_to_openai_model_list_for_vllm():
    import httpx

    def handler(request):
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"data": [{"id": "m"}]})
        return httpx.Response(404, json={"detail": "Not Found"})

    status = asyncio.run(model_status._probe("m", transport=httpx.MockTransport(handler)))
    assert status.backend == "openai-compatible" and status.loaded is True and status.vision is None


# ---------------------------------------------------------------------------
# Intent recovery: a tool call described as text is re-generated as a real call
# ---------------------------------------------------------------------------

class _SequenceEngine:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    async def create_chat_completion(self, messages, **kwargs):
        self.calls.append(kwargs)
        return self.replies.pop(0)


def _text(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _tool_reply(name, arguments):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}]}}]}


def test_described_tool_call_is_regenerated_as_a_required_call(monkeypatch):
    engine = _SequenceEngine(
        _text("شنو رأيك نستدعي الدالة `searchShipments` ويا رقم الوصل؟"),
        _tool_reply("searchShipments", "{\"receiptNumber\":\"12345\"}"),
    )
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request(
        [{"role": "user", "content": "وين شحنتي 12345؟"}], [_shipment_tool()])))

    assert [c["tool_choice"] for c in engine.calls] == ["auto", "required"]
    assert response["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "searchShipments"


def test_no_regeneration_after_a_tool_result(monkeypatch):
    engine = _SequenceEngine(_text("حسب searchShipments شحنتك ببغداد."))
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request([
        {"role": "user", "content": "وين شحنتي؟"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
            "function": {"name": "searchShipments", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "{\"stateName\":\"بغداد\"}"},
    ], [_shipment_tool()])))

    assert len(engine.calls) == 1
    assert response["choices"][0]["message"]["content"] == "حسب searchShipments شحنتك ببغداد."


def test_no_regeneration_when_no_tool_is_named(monkeypatch):
    engine = _SequenceEngine(_text("هلا بيك، شلون أكدر أساعدك؟"))
    monkeypatch.setattr(compat, "llm_engine", engine)

    asyncio.run(compat.chat_completions(_request(
        [{"role": "user", "content": "هلو"}], [_shipment_tool()])))

    assert len(engine.calls) == 1


def test_no_regeneration_for_a_name_that_is_only_a_substring(monkeypatch):
    engine = _SequenceEngine(_text("searchShipmentsByAgent غير موجودة"))
    monkeypatch.setattr(compat, "llm_engine", engine)

    asyncio.run(compat.chat_completions(_request(
        [{"role": "user", "content": "هلو"}], [_shipment_tool()])))

    assert len(engine.calls) == 1


def test_tool_intent_without_name_is_regenerated_when_a_spreadsheet_is_attached(monkeypatch):
    engine = _SequenceEngine(
        _text("لازم أستدعي الأداة مال التكرارات. شنو رأيك نستعملها؟"),
        _tool_reply("searchShipments", "{}"),
    )
    monkeypatch.setattr(compat, "llm_engine", engine)

    asyncio.run(compat.chat_completions(_request([{"role": "user", "content":
        "Find duplicated rows.\n\n[Attachment] spreadsheet attachmentId=31 file=\"t.xlsx\""}],
        [_shipment_tool()])))

    assert [c["tool_choice"] for c in engine.calls] == ["auto", "required"]


def test_tool_intent_without_spreadsheet_is_left_alone(monkeypatch):
    engine = _SequenceEngine(_text("حتى أستعمل الأداة أحتاج رقم الوصل، تكدر تنطيني إياه؟"))
    monkeypatch.setattr(compat, "llm_engine", engine)

    asyncio.run(compat.chat_completions(_request(
        [{"role": "user", "content": "وين شحنتي؟"}], [_shipment_tool()])))

    assert len(engine.calls) == 1


_TOOL_TURN = [
    {"role": "user", "content": "Which row has a missing Amount?"},
    {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
        "function": {"name": "searchShipments", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "c1", "content": "{\"rows\":[{\"rowNumber\":5}]}"},
]


def test_empty_reply_after_tool_result_is_regenerated_as_text(monkeypatch):
    engine = _SequenceEngine(_text(""), _text("الصف 5 ناقص."))
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request(_TOOL_TURN, [_shipment_tool()])))

    assert [c["tool_choice"] for c in engine.calls] == ["auto", "none"]
    assert response["choices"][0]["message"]["content"] == "الصف 5 ناقص."


def test_empty_reply_is_retried_only_once(monkeypatch):
    engine = _SequenceEngine(_text(""), _text(""))
    monkeypatch.setattr(compat, "llm_engine", engine)

    response = asyncio.run(compat.chat_completions(_request(_TOOL_TURN, [_shipment_tool()])))

    assert len(engine.calls) == 2
    assert response["choices"][0]["message"]["content"] == EXHAUSTED_FALLBACK


def test_asking_which_columns_is_regenerated_when_a_spreadsheet_is_attached(monkeypatch):
    engine = _SequenceEngine(
        _text("أحتاج أعرف أي الأعمدة اللي تريدني أقارن بيها بالضبط؟"),
        _tool_reply("searchShipments", "{}"),
    )
    monkeypatch.setattr(compat, "llm_engine", engine)

    asyncio.run(compat.chat_completions(_request([{"role": "user", "content":
        "Find duplicated rows.\n\n[Attachment] spreadsheet attachmentId=31 file=\"t.xlsx\""}],
        [_shipment_tool()])))

    assert [c["tool_choice"] for c in engine.calls] == ["auto", "required"]
