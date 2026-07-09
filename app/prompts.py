# -*- coding: utf-8 -*-
"""بناء برومبت بصيغة ChatML (متوافقة مع Qwen2.5-Instruct) مع دمج سياق RAG."""

from typing import Dict, List

from app.config import settings


def _rag_context_block(rag_results: List[dict]) -> str:
    if not rag_results:
        return ""
    lines = []
    for r in rag_results:
        if r.get("word"):
            lines.append(f"- {r['word']}: {r['meaning']}")
        else:
            lines.append(f"- {r['text']}")
    return "\n\nمعلومات مرجعية عن اللهجة العراقية (استخدمها إذا كانت مفيدة):\n" + "\n".join(lines)


def build_prompt(
    history: List[Dict[str, str]],
    user_message: str,
    rag_results: List[dict],
) -> str:
    """system (ثابت) + سياق RAG + تاريخ المحادثة + الرسالة الجديدة.

    ثبات صياغة الـ system prompt يسمح لـ Prefix Caching في vLLM بإعادة استخدام
    الـ KV cache لنفس المقطع بين الطلبات المتتالية بدل إعادة حسابه.
    """
    system_block = settings.system_prompt + _rag_context_block(rag_results)
    parts = [f"<|im_start|>system\n{system_block}<|im_end|>\n"]

    for turn in history:
        parts.append(f"<|im_start|>{turn['role']}\n{turn['content']}<|im_end|>\n")

    parts.append(f"<|im_start|>user\n{user_message}<|im_end|>\n<|im_start|>assistant\n")
    return "".join(parts)
