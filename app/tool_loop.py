# -*- coding: utf-8 -*-
"""حلقة استدعاء أدوات نصّية عامة، مشتركة بين أي ميزة تحتاج الموديل يستدعي أداة
(بحث ويب، تتبع طلب...) أثناء توليد رده.

لا نعتمد على tool-calling الأصلي لأي محرك (غير مؤكّد الدعم لموديل حديث جداً مثل
Gemma 4) — بدلاً منه نطلب من الموديل (عبر system prompt) إخراج كتلة نصية
بالشكل:

    [TOOL_CALL]{"tool": "اسم_الأداة", "args": {...}}[/TOOL_CALL]

ونمرر "[/TOOL_CALL]" كـ stop string حتى يتوقف التوليد هناك بالضبط
ولا يصل أي جزء من طلب الأداة للعميل مباشرة. النمط نفسه المستخدم أصلاً لعلامة
[ORDER_READY] بميزة المبيعات، معمَّم هنا لأي عدد من الأدوات.
"""

import json
import re
from typing import Awaitable, Callable, Dict, List, Optional

from app.engine import llm_engine

_TOOL_CALL_TAIL = re.compile(r"\[TOOL_CALL\]\s*(\{.*\})\s*$", re.DOTALL)

ToolFunc = Callable[[dict], Awaitable[dict]]


async def run_with_tools(
    messages: List[Dict[str, str]],
    tools: Dict[str, ToolFunc],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    extra_stop: Optional[List[str]] = None,
    max_rounds: int = 3,
) -> str:
    """يولّد رداً؛ إذا طلب الموديل أداة يجهّز نتيجتها ويعيد التوليد، حتى رد
    نهائي بدون طلب أداة أو بلوغ `max_rounds` (تفادي حلقة لا نهائية)."""
    stop = ["[/TOOL_CALL]"] + (extra_stop or [])
    working_messages = list(messages)
    text = ""

    for _ in range(max_rounds):
        prompt = llm_engine.render_prompt(working_messages)
        result_holder: dict = {}
        text = await llm_engine.generate_full(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            stop=stop,
            result_holder=result_holder,
        )

        if result_holder.get("stop_reason") != "[/TOOL_CALL]":
            return text.strip()

        match = _TOOL_CALL_TAIL.search(text)
        if not match:
            return text.strip()

        try:
            call = json.loads(match.group(1))
        except json.JSONDecodeError:
            return text.strip()

        tool_name = call.get("tool")
        args = call.get("args") or {}
        tool_func = tools.get(tool_name)
        if tool_func is None:
            tool_result = {"error": f"أداة غير معروفة: {tool_name}"}
        else:
            try:
                tool_result = await tool_func(args)
            except Exception as exc:  # لا نكسر المحادثة إذا فشلت أداة خارجية
                tool_result = {"error": str(exc)}

        visible_text = text[: match.start()].strip()
        working_messages.append({"role": "assistant", "content": visible_text or "..."})
        working_messages.append({
            "role": "user",
            "content": (
                f"[نتيجة الأداة {tool_name}]: {json.dumps(tool_result, ensure_ascii=False)}\n"
                "تابع ردك للعميل بالاعتماد على هذه النتيجة."
            ),
        })

    return text.strip()
