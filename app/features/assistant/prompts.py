# -*- coding: utf-8 -*-
"""مساعد عام باللهجة العراقية — system prompt ثابت + سياق RAG للهجة."""

from typing import Dict, List

from app.context_blocks import words_context_block

Message = Dict[str, str]

ASSISTANT_SYSTEM_PROMPT = (
    "أنت مساعد ذكي يتحدث ويفهم اللهجة العراقية. "
    "أجب بإيجاز ووضوح، واستخدم المعلومات المرجعية إن كانت مفيدة."
)


def build_prompt(history: List[Message], user_message: str, rag_words: List[dict]) -> List[Message]:
    system_content = ASSISTANT_SYSTEM_PROMPT + words_context_block(rag_words)
    messages: List[Message] = [{"role": "system", "content": system_content}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_message})
    return messages
