# -*- coding: utf-8 -*-
"""مساعد الموظف النصي بعقد OpenAI: الموظف يكتب "اتصل بهؤلاء" بتطبيق سطح المكتب.

جولة توليد واحدة بلا حالة (نفس نمط مسارات الأدوات بالخدمة): التطبيق يرسل
التاريخ واسم الشركة (`company_name`) وأدواته اللي ينفّذها هو (جهات الاتصال،
الواتساب، الحملات، التذاكر...)، والخادم يضيف أداة start_call. النموذج يرجع
tool_calls، والتطبيق ينفّذها:
  - أدواته ← نظامه، ويعيد النتيجة برسالة role="tool".
  - start_call ← يملأ company_name ثم POST /voice_followup/calls، فتبدأ المكالمة.

أدوات البيانات من باك اند Catalog (operator_tools.py: search_orders،
search_products) ينفّذها الخادم نفسه بحلقة داخل نفس الطلب؛ الرد للتطبيق ما
يحتوي استدعاءاتها أبداً — بس نص نهائي أو tool_calls لأدوات التطبيق/start_call.

نسخة خاصة بهذه الميزة من منطق التطبيع (مستقلة عن openai_compat عمداً): كل
ميزة تتغيّر وحدها بلا ما تأثر على غيرها.
"""

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.config import settings
from app.core.engine import (
    AI_MODEL_NOT_LOADED,
    AI_REQUEST_TIMEOUT,
    AI_UPSTREAM_ERROR,
    LM_STUDIO_UNAVAILABLE,
    LLMEngine,
    LLMUpstreamError,
    _MODEL_NOT_LOADED_HINTS,
    _upstream_error_message,
)
from app.core.dialect import recent_user_text, understanding_block
from app.features.voice_followup import operator_tools
from app.features.voice_followup.prompts import START_CALL_TOOL, START_CALL_TOOL_NAME, build_operator_system_prompt

logger = logging.getLogger(__name__)

# تفكير منخفض: بدونه الموديل يسأل عن تفاصيل بدل ما يستدعي الأداة اللي عنده.
REASONING_EFFORT = "low"
FALLBACK_REPLY = "صار خلل بالمعالجة، ممكن تعيد الطلب؟"
# أقصى جولات أدوات خادم قبل ما نجبر النموذج يرد بلاها.
MAX_SERVER_TOOL_ROUNDS = 3


class OperatorFunction(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)


class OperatorTool(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["function"]
    function: OperatorFunction


class OperatorToolCallFunction(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    arguments: Any = "{}"


class OperatorToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: Literal["function"] = "function"
    function: OperatorToolCallFunction


class OperatorMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: Any = None
    tool_calls: Optional[List[OperatorToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class OperatorChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[OperatorMessage] = Field(min_length=1)
    tools: Optional[List[OperatorTool]] = None
    tool_choice: Any = "auto"
    stream: bool = False
    max_tokens: Optional[int] = Field(default=None, gt=0)
    # اسم الشركة من إعدادات التطبيق — يدخل رسالة system للمساعد.
    company_name: Optional[str] = Field(default=None, max_length=120)


def _content_as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") == "text"]
        if parts and all(isinstance(p, str) for p in parts):
            return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False)


def build_messages(messages: List[OperatorMessage], company_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """رسالة system واحدة (المساعد + أي system يرسلها التطبيق) ثم الباقي كما هو."""
    rest = list(messages)
    leading = []
    while rest and rest[0].role == "system":
        text = _content_as_text(rest.pop(0).content)
        if text:
            leading.append(text)
    prompt = build_operator_system_prompt(company_name)
    system = "\n\n".join([prompt, *leading]) + understanding_block(recent_user_text(rest))
    converted = [{"role": "system", "content": system}]
    for message in rest:
        native = message.model_dump(exclude_none=True)
        if "content" in native:
            native["content"] = _content_as_text(native["content"])
        converted.append(native)
    return converted


def build_tools(client_tools: Optional[List[OperatorTool]], server_tools: Optional[List[dict]] = None) -> List[dict]:
    """أدوات التطبيق + start_call + أدوات بيانات الخادم (تعريف الخادم يحل محل
    أداة تطبيق بنفس الاسم)."""
    server_tools = server_tools or []
    reserved = {START_CALL_TOOL_NAME} | {t["function"]["name"] for t in server_tools}
    tools = [t.model_dump(exclude_none=True) for t in (client_tools or [])
             if t.function.name not in reserved]
    return tools + [START_CALL_TOOL] + server_tools


async def _completion(engine: Any, messages: list, tools: list, tool_choice: Any,
                      max_tokens: Optional[int]) -> dict:
    if not isinstance(engine, LLMEngine):
        return await engine.create_chat_completion(
            messages, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens,
            reasoning_effort=REASONING_EFFORT,
        )
    if not engine.ready or engine._client is None:
        raise LLMUpstreamError("خادم النموذج غير متاح حالياً", code=LM_STUDIO_UNAVAILABLE)
    body = engine._build_body(
        messages, max_tokens or settings.max_new_tokens, None, None,
        tools=tools, tool_choice=tool_choice,
    )
    body["reasoning_effort"] = REASONING_EFFORT
    body["chat_template_kwargs"] = {"enable_thinking": True}
    try:
        resp = await engine._client.post("/chat/completions", json=body)
    except httpx.TimeoutException as exc:
        raise LLMUpstreamError(
            f"انتهت مهلة خادم النموذج ({settings.llm_request_timeout_seconds:g} ثانية)",
            code=AI_REQUEST_TIMEOUT,
        ) from exc
    except httpx.HTTPError as exc:
        raise LLMUpstreamError(f"تعذّر الاتصال بخادم النموذج: {exc}", code=LM_STUDIO_UNAVAILABLE) from exc
    if resp.status_code >= 400:
        message = _upstream_error_message(resp)
        code = (AI_MODEL_NOT_LOADED if any(h in message.lower() for h in _MODEL_NOT_LOADED_HINTS)
                else AI_UPSTREAM_ERROR)
        raise LLMUpstreamError(message, upstream_status=resp.status_code, code=code)
    return resp.json()


# الموديل يسرّب أحياناً بنية قنوات القالب داخل النص: "<|channel>thought\n<channel|>الجواب".
# الجواب الفعلي بعد آخر وسم إغلاق قناة.
_CHANNEL_CLOSE_RE = re.compile(r"<\|?channel\|>")
_STRAY_TAG_RE = re.compile(r"<\|[^<>]*\|?>|<[^<>]*\|>")


def clean_content(text: str) -> str:
    cleaned = text
    matches = list(_CHANNEL_CLOSE_RE.finditer(cleaned))
    if matches and cleaned[matches[-1].end():].strip():
        cleaned = cleaned[matches[-1].end():]
    cleaned = _STRAY_TAG_RE.sub("", cleaned)
    return re.sub(r"[ \t]*\n[ \t]*\n+", "\n\n", cleaned).strip()


def normalized_tool_calls(raw_calls: Any, allowed_names: set) -> Optional[List[dict]]:
    """tool_calls صالحة بأسماء مرسلة فعلاً ووسائط كائن JSON، أو None."""
    if not isinstance(raw_calls, list) or not raw_calls:
        return None
    normalized, seen_ids = [], set()
    for raw in raw_calls:
        function = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(function, dict):
            return None
        name, arguments = function.get("name"), function.get("arguments", "{}")
        if not isinstance(name, str) or name not in allowed_names:
            return None
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else None
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        call_id = raw.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
            call_id = f"call_{uuid.uuid4().hex}"
        seen_ids.add(call_id)
        normalized.append({"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}})
    return normalized


def _response(model: str, message: dict, finish_reason: str, data: Optional[dict] = None) -> dict:
    usage = (data or {}).get("usage") if isinstance(data, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {k: usage.get(k) or 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
    }


def _parse(data: Any, model: str, allowed: set) -> Optional[dict]:
    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    if not isinstance(message, dict):
        return None
    tool_calls = normalized_tool_calls(message.get("tool_calls"), allowed)
    if tool_calls:
        return _response(model, {"role": "assistant", "content": None, "tool_calls": tool_calls}, "tool_calls", data)
    content = message.get("content")
    text = clean_content(content) if isinstance(content, str) else ""
    if text:
        return _response(model, {"role": "assistant", "content": text}, "stop", data)
    return None


async def _model_turn(engine: Any, req: OperatorChatRequest, messages: list, tools: list,
                      first_round: bool) -> Optional[dict]:
    allowed = {t["function"]["name"] for t in tools}
    data = await _completion(engine, messages, tools, req.tool_choice, req.max_tokens)
    response = _parse(data, req.model, allowed)
    if response is None:
        # رد فارغ: إعادة واحدة؛ بعد نتيجة أداة التطبيق المطلوب نص ملخّص فنمنع أداة جديدة.
        retry_choice = "none" if first_round and req.messages[-1].role == "tool" else req.tool_choice
        data = await _completion(engine, messages, tools, retry_choice, req.max_tokens)
        response = _parse(data, req.model, allowed)
    if response is None:
        logger.warning("رد غير صالح بمسار مساعد الموظف: %r", data)
    return response


async def run_operator_turn(engine: Any, req: OperatorChatRequest) -> dict:
    server_tools = operator_tools.active_tools()
    server_names = {t["function"]["name"] for t in server_tools}
    tools = build_tools(req.tools, server_tools)
    messages = build_messages(req.messages, req.company_name)
    for round_no in range(MAX_SERVER_TOOL_ROUNDS + 1):
        # آخر جولة بلا أدوات الخادم: النموذج لازم يرد نصاً أو بأدوات التطبيق.
        round_tools = tools if round_no < MAX_SERVER_TOOL_ROUNDS else [
            t for t in tools if t["function"]["name"] not in server_names]
        response = await _model_turn(engine, req, messages, round_tools, round_no == 0)
        if response is None:
            break
        calls = response["choices"][0]["message"].get("tool_calls") or []
        server_calls = [c for c in calls if c["function"]["name"] in server_names]
        if not server_calls:
            return response
        # أدوات الخادم تتنفّذ هنا؛ أي أداة تطبيق بنفس الرد تنعاد بعد ما يشوف النموذج النتائج.
        messages.append({"role": "assistant", "content": None, "tool_calls": server_calls})
        for call in server_calls:
            result = await operator_tools.run(call["function"]["name"], call["function"]["arguments"])
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    return _response(req.model, {"role": "assistant", "content": FALLBACK_REPLY}, "stop")


async def completion_events(response: dict):
    """نفس الرد بصيغة OpenAI SSE (chunk واحد ثم finish ثم [DONE])."""
    base = {key: response[key] for key in ("id", "created", "model")}
    base["object"] = "chat.completion.chunk"
    choice = response["choices"][0]
    delta = dict(choice["message"])
    if "tool_calls" in delta:
        delta["tool_calls"] = [dict(call, index=i) for i, call in enumerate(delta["tool_calls"])]
    for value, finish in ((delta, None), ({}, choice["finish_reason"])):
        chunk = dict(base, choices=[{"index": 0, "delta": value, "finish_reason": finish}])
        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"
