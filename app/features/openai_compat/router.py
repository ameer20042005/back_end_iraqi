# -*- coding: utf-8 -*-
"""OpenAI-compatible chat-completions adapter for the jbot agent.

This route performs exactly one native tool-aware model generation. jbot owns
conversation state and tool execution; this service validates and normalizes
the OpenAI wire response returned by vLLM.
"""

import json
import logging
import re
import time
import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.engine import llm_engine
from app.fallback import EXHAUSTED_FALLBACK
from app.features.openai_compat.auth import require_openai_compat_api_key
from app.features.openai_compat.prompts import OPENAI_COMPAT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["openai-compatible"])


class FunctionDefinition(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: Optional[str] = None
    parameters: Dict[str, Any] = Field(default_factory=dict)


class ChatTool(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: Literal["function"]
    function: FunctionDefinition


class FunctionCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    arguments: Any = "{}"


class AssistantToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: Literal["function"] = "function"
    function: FunctionCall


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: Literal["system", "user", "assistant", "tool"]
    content: Any = None
    tool_calls: Optional[List[AssistantToolCall]] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    # Spring AI/OpenAI clients may send optional standard fields that this
    # deterministic model does not use. Accept them to preserve wire
    # compatibility instead of rejecting an otherwise valid request.
    model_config = ConfigDict(extra="allow")

    model: str
    messages: List[ChatMessage] = Field(min_length=1)
    tools: Optional[List[ChatTool]] = None
    tool_choice: Any = "auto"
    stream: bool = False


def _content_as_text(content: Any) -> str:
    """Render OpenAI message content as text without interpreting tool JSON."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if text_parts and all(isinstance(part, str) for part in text_parts):
            return "\n".join(part for part in text_parts if part)
    return json.dumps(content, ensure_ascii=False)


def _native_messages(messages: List[ChatMessage]) -> List[Dict[str, Any]]:
    """Prepend the owner prompt and preserve native OpenAI tool messages."""
    converted: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": OPENAI_COMPAT_SYSTEM_PROMPT,
        },
    ]
    for message in messages:
        native = message.model_dump(exclude_none=True)
        if "content" in native:
            native["content"] = _content_as_text(native["content"])
        converted.append(native)

    return converted


# رموز قالب المحادثة التي يسرّبها الموديل داخل نص الرد نفسه، بالشكل:
#
#     <|channel>thought\n<channel|>النص الحقيقي للجواب
#
# هي بنية القالب الداخلية لا جزء من الجواب، لكنها تصل العميل كنص عادي فيعرضها
# للمستخدم كما هي. لاحظ أن ما بين الوسمين ("thought") اسم القناة، فحذف الوسمين
# وحدهما يترك الكلمة معلّقة في أول الجواب.
#
# ليش التنظيف هنا لا بجهة jbot؟ لأن هذه النقطة هي حدود الخدمة: كل عميل يستهلك
# منها يتوقّع نصاً نظيفاً، وتكرار المعالجة بكل عميل يعني نسيانها بأحدهم.
#
# القاعدة: الجواب الفعلي يقع بعد **آخر** وسم إغلاق قناة. هذا يعالج حالة القناة
# الواحدة وحالة القنوات المتتابعة (thought ثم final) بنفس السطر، بدل افتراض
# ترتيب ثابت.
_CHANNEL_CLOSE_RE = re.compile(r"<\|?channel\|>")

# وسوم متفرّقة قد تبقى خارج بنية القناة: <|...|> أو <|...> أو <...|>.
_STRAY_TAG_RE = re.compile(r"<\|[^<>]*\|?>|<[^<>]*\|>")


def _clean_content(text: str) -> str:
    """يزيل بنية قنوات القالب المتسرّبة ويشذّب ما تخلّفه من مسافات."""
    cleaned = text
    matches = list(_CHANNEL_CLOSE_RE.finditer(cleaned))
    if matches:
        after = cleaned[matches[-1].end():]
        # لو لم يبق نص بعد آخر وسم فالبنية غير متوقّعة: نبقي الأصل وننظّف
        # الوسوم فقط، فنصّ ناقص أسوأ من نص فيه رمز شارد.
        if after.strip():
            cleaned = after

    cleaned = _STRAY_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]*\n[ \t]*\n+", "\n\n", cleaned).strip()

    if cleaned != text.strip():
        logger.info("نُظّفت رموز قالب من رد الموديل (%d حرفاً)", len(text.strip()) - len(cleaned))
    return cleaned


def _base_response(requested_model: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _final_response(requested_model: str, content: str) -> dict:
    response = _base_response(requested_model)
    response["choices"] = [{
        "index": 0,
        "message": {"role": "assistant", "content": content},
        "finish_reason": "stop",
    }]
    return response


def _usage(data: dict) -> dict:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        usage = {}
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
    }


def _normalized_tool_calls(raw_calls: Any, allowed_names: set[str]) -> Optional[List[dict]]:
    if not isinstance(raw_calls, list) or not raw_calls:
        return None
    normalized = []
    seen_ids = set()
    for raw_call in raw_calls:
        if not isinstance(raw_call, dict):
            return None
        function = raw_call.get("function")
        if not isinstance(function, dict):
            return None
        name = function.get("name")
        arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or name not in allowed_names:
            return None
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        if not isinstance(arguments, str):
            return None
        try:
            json.loads(arguments)
        except json.JSONDecodeError:
            return None

        call_id = raw_call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in seen_ids:
            call_id = f"call_{uuid.uuid4().hex}"
        seen_ids.add(call_id)
        normalized.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    return normalized


@router.post("/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    _api_key: str = Depends(require_openai_compat_api_key),
):
    """Run one native tool-aware generation and return an OpenAI response."""
    tools = [tool.model_dump(exclude_none=True) for tool in (req.tools or [])]
    messages = _native_messages(req.messages)
    data = await llm_engine.create_chat_completion(
        messages,
        tools=tools,
        tool_choice=req.tool_choice,
    )

    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(message, dict):
        allowed_names = {tool["function"]["name"] for tool in tools}
        tool_calls = _normalized_tool_calls(message.get("tool_calls"), allowed_names)
        if tool_calls:
            response = _base_response(req.model)
            response["usage"] = _usage(data)
            response["choices"] = [{
                "index": 0,
                "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
                "finish_reason": "tool_calls",
            }]
            return response

        content = message.get("content")
        cleaned = _clean_content(content) if isinstance(content, str) else ""
        if cleaned:
            response = _final_response(req.model, cleaned)
            response["usage"] = _usage(data)
            return response

    logger.warning("رد vLLM الأصلي غير صالح لمسار OpenAI-compatible: %r", data)
    return _final_response(req.model, EXHAUSTED_FALLBACK)
