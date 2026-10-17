# -*- coding: utf-8 -*-
"""Sales-agent inference for the Spring ai_agent (``POST /sales/agent/chat/completions``).

ai_agent owns conversation state, tool definitions and tool execution; this
module performs one native tool-aware generation with the sales persona.

Inference choices, measured on gemma-4-e4b against the real ai_agent requests:
- one system message (Gemma's template has a single leading system turn);
- message content parts (``image_url``) forwarded untouched so the model sees photos;
- low reasoning for this route only: correct tool decisions at action moments
  (preparing an order after the customer gave their details, searching after a
  product photo) went from ~50% without thinking to 10/10, for a bounded extra delay.
The low-reasoning request itself is built by app/features/openai_compat/completion.py,
shared with the jbot route; the engine's default policy for other features is unchanged.
"""

import logging
from typing import Any, Dict, List, Optional

from app.core.dialect import dialect_block, recent_user_text
from app.core.engine import llm_engine
from app.core.fallback import EXHAUSTED_FALLBACK
from app.features.openai_compat.completion import create_completion
from app.features.openai_compat.router import (
    ChatCompletionRequest,
    ChatMessage,
    _base_response,
    _clean_content,
    _content_as_text,
    _final_response,
    _is_empty_reply,
    _normalized_tool_calls,
    _usage,
)
from app.features.sales.prompts import SALES_AGENT_SYSTEM_PROMPT

logger = logging.getLogger(__name__)


def agent_messages(messages: List[ChatMessage]) -> List[Dict[str, Any]]:
    """رسالة system واحدة (شخصية المبيعات + رسائل system الأولى من ai_agent) ثم بقية الرسائل كما هي."""
    rest = list(messages)
    leading = []
    while rest and rest[0].role == "system":
        text = _content_as_text(rest.pop(0).content)
        if text:
            leading.append(text)
    system = "\n\n".join([SALES_AGENT_SYSTEM_PROMPT, *leading]) + dialect_block(recent_user_text(rest))
    return [
        {"role": "system", "content": system},
        *(message.model_dump(exclude_none=True) for message in rest),
    ]


async def run_agent_turn(req: ChatCompletionRequest, max_tokens: Optional[int]) -> dict:
    """جولة توليد واحدة وتطبيع ردها بصيغة OpenAI (tool_calls بأسماء أرسلها العميل فقط، أو نص نظيف)."""
    tools = [tool.model_dump(exclude_none=True) for tool in (req.tools or [])]
    messages = agent_messages(req.messages)
    data = await create_completion(llm_engine, messages, tools, req.tool_choice, max_tokens)

    if _is_empty_reply(data):
        # رد فارغ تماماً (بلا نص ولا أدوات): إعادة واحدة؛ بعد نتيجة أداة المطلوب نص فنمنع استدعاءً جديداً.
        retry_choice = "none" if req.messages[-1].role == "tool" else req.tool_choice
        logger.info("رد فارغ من الموديل بمسار وكيل المبيعات؛ إعادة واحدة بـ tool_choice=%s", retry_choice)
        data = await create_completion(llm_engine, messages, tools, retry_choice, max_tokens)

    choices = data.get("choices") if isinstance(data, dict) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    if isinstance(message, dict):
        tool_calls = _normalized_tool_calls(message.get("tool_calls"), {t["function"]["name"] for t in tools})
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

    logger.warning("رد خادم النموذج غير صالح لمسار وكيل المبيعات: %r", data)
    return _final_response(req.model, EXHAUSTED_FALLBACK)
