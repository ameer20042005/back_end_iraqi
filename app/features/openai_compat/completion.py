# -*- coding: utf-8 -*-
"""One native tool-aware completion with bounded (low) reasoning, shared by the tool-calling routes.

Why: without thinking the model often answers "I need more details" instead of
calling the tool it was given. Measured on gemma-4-e4b with the real requests:
- jbot ``searchShipments`` for "وين وصلت شحنتي 12345؟": 0/5 without thinking, 5/5 with low reasoning;
- sales-agent action moments (prepare order after details, search after a photo): ~50% → 10/10.

The engine's shared policy stays "no thinking" for the other features (order intake,
voice, district correction), so this module builds the request body itself from the
engine's own body builder and HTTP client, with identical error semantics.
"""

from typing import Any, Dict, List, Optional

import httpx

from app.config import settings
from app.engine import (
    AI_MODEL_NOT_LOADED,
    AI_REQUEST_TIMEOUT,
    AI_UPSTREAM_ERROR,
    LM_STUDIO_UNAVAILABLE,
    LLMEngine,
    LLMUpstreamError,
    _MODEL_NOT_LOADED_HINTS,
    _upstream_error_message,
)

REASONING_EFFORT = "low"


async def create_completion(
    engine: Any,
    messages: List[Dict[str, Any]],
    tools: List[dict],
    tool_choice: Any,
    max_tokens: Optional[int] = None,
) -> dict:
    """Return one OpenAI chat completion from ``engine`` with low reasoning enabled."""
    if not isinstance(engine, LLMEngine):
        # Any other engine-like object (e.g. a test double) receives the same request through its
        # public method, with the reasoning setting passed explicitly.
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
