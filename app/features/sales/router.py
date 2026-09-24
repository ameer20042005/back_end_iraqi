# -*- coding: utf-8 -*-
"""OpenAI-compatible, stateless model endpoint for the sales persona."""

import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import Field

from app.engine import llm_engine
from app.fallback import EXHAUSTED_FALLBACK
from app.features.openai_compat.router import (
    ChatCompletionRequest,
    _base_response,
    _clean_content,
    _final_response,
    _normalized_tool_calls,
    _usage,
)
from app.features.sales.auth import require_sales_api_key
from app.features.sales.prompts import build_sales_prompt

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sales", tags=["sales"])


class SalesChatRequest(ChatCompletionRequest):
    max_tokens: Optional[int] = Field(default=None, gt=0)


def _native_messages(messages) -> list[dict]:
    """Prepend the sales owner prompt and preserve native tool messages."""
    return build_sales_prompt([
        message.model_dump(exclude_none=True) for message in messages
    ])


def _completion_response(model: str, content, tool_calls, data: dict) -> dict:
    if tool_calls:
        response = _base_response(model)
        response["choices"] = [{
            "index": 0,
            "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
            "finish_reason": "tool_calls",
        }]
    else:
        response = _final_response(model, content)
    response["usage"] = _usage(data)
    return response


async def _completion_events(response: dict):
    base = {key: response[key] for key in ("id", "created", "model")}
    base["object"] = "chat.completion.chunk"
    choice = response["choices"][0]
    delta = dict(choice["message"])
    if "tool_calls" in delta:
        delta["tool_calls"] = [dict(call, index=index) for index, call in enumerate(delta["tool_calls"])]
    for value, finish_reason in ((delta, None), ({}, choice["finish_reason"])):
        chunk = dict(
            base,
            choices=[{"index": 0, "delta": value, "finish_reason": finish_reason}],
        )
        yield "data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n"
    yield "data: [DONE]\n\n"


@router.post("/chat/completions")
@router.post("/chat", include_in_schema=False)
async def sales_chat(
    req: SalesChatRequest,
    _api_key: str = Depends(require_sales_api_key),
):
    """Run one native tool-aware generation and return an OpenAI response."""
    tools = [tool.model_dump(exclude_none=True) for tool in (req.tools or [])]
    messages = _native_messages(req.messages)
    data = await llm_engine.create_chat_completion(
        messages,
        tools=tools,
        tool_choice=req.tool_choice,
        max_tokens=req.max_tokens,
    )

    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    response = None
    if isinstance(message, dict):
        allowed_names = {tool["function"]["name"] for tool in tools}
        tool_calls = _normalized_tool_calls(message.get("tool_calls"), allowed_names)
        if tool_calls:
            response = _completion_response(req.model, None, tool_calls, data)
        else:
            content = message.get("content")
            cleaned = _clean_content(content) if isinstance(content, str) else ""
            if cleaned:
                response = _completion_response(req.model, cleaned, None, data)

    if response is None:
        logger.warning("رد vLLM الأصلي غير صالح لمسار المبيعات: %r", data)
        response = _final_response(req.model, EXHAUSTED_FALLBACK)

    if req.stream:
        return StreamingResponse(
            _completion_events(response),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return response


@router.post("/chat/stream", include_in_schema=False)
async def sales_chat_stream(
    req: SalesChatRequest,
    _api_key: str = Depends(require_sales_api_key),
):
    """Compatibility name; emits the same checked OpenAI SSE response."""
    return await sales_chat(req.model_copy(update={"stream": True}), _api_key)
