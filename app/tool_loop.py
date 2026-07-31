# -*- coding: utf-8 -*-
"""حلقة استدعاء أدوات عامة، مشتركة بين أي ميزة تحتاج الموديل يستدعي أداة
(بحث منتجات، تتبع طلب...) أثناء توليد رده.

البروتوكول: guided_json (structured outputs بجهة vLLM — انظر app/engine.py)
يقيّد كل رد من الموديل بمخطط JSON صارم واحد:

    {
      "action": "tool_call" | "final_answer",
      "tool_call": {"tool": "اسم_الأداة", "args": {...}},
      "final_answer": "..."
    }

الموديل يقرر بنفسه: يحتاج بيانات؟ يرجع action="tool_call" باسم الأداة
ومعاملاتها، الباك اند ينفّذها (استعلام حقيقي من البيانات) ويعيد النتيجة
كرسالة جديدة، والموديل يحلّلها بجولة توليد ثانية حتى يوصل action="final_answer".
هذا يستبدل بروتوكول [TOOL_CALL]{...}[/TOOL_CALL] النصي القديم: guided decoding
يضمن JSON صالحاً فعلياً (مقيَّد وقت التوليد نفسه) بدل تحليل نص حر عرضة
للانحراف عن الصيغة.
"""

import json
import logging
from typing import Awaitable, Callable, Dict, List, Optional

from app.engine import llm_engine

logger = logging.getLogger(__name__)

# مخطط الرد الأساسي لكل الميزات اللي تستعمل حلقة الأدوات. ميزة تحتاج حقولاً
# إضافية بردها النهائي (مثل order_ready بالمبيعات) تمررها بـ
# `extra_properties`/`extra_required` بدل تفرّع مخطط منفصل.
_BASE_PROPERTIES = {
    "action": {"type": "string", "enum": ["tool_call", "final_answer"]},
    "tool_call": {
        "type": "object",
        "properties": {
            "tool": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["tool"],
    },
    "final_answer": {"type": "string"},
}


def build_schema(extra_properties: Optional[dict] = None, extra_required: Optional[List[str]] = None) -> dict:
    """يبني مخطط guided_json لحلقة الأدوات، بحقول إضافية اختيارية فوق
    action/tool_call/final_answer الأساسية."""
    return {
        "type": "object",
        "properties": {**_BASE_PROPERTIES, **(extra_properties or {})},
        "required": ["action"] + (extra_required or []),
    }


TOOL_LOOP_SCHEMA = build_schema()

# رد احتياطي حين تنفد الجولات بلا إجابة نهائية.
_EXHAUSTED_FALLBACK = (
    "معذرة، صار عندي تعقيد بجلب المعلومة. عطيني رقم الطلب (مثل ORD-1001) أو "
    "رقم هاتفك وأتابعلك مباشرة."
)

ToolFunc = Callable[[dict], Awaitable[dict]]


def _parse_action(text: str) -> Optional[dict]:
    """يفكّك رد الموديل المقيَّد بـ TOOL_LOOP_SCHEMA. guided_json يضمن JSON
    صالحاً نحوياً، لكن نبقى متحفّظين على شكل الحقول (قد تُفقد قيمة رغم
    صلاحية الـ JSON إذا انحرف الموديل عن enum بطريقة ما)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "action" not in data:
        return None
    return data


async def run_with_tools(
    messages: List[Dict[str, str]],
    tools: Dict[str, ToolFunc],
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    max_rounds: int = 3,
    schema: Optional[dict] = None,
) -> dict:
    """يولّد رداً مقيَّداً بـ `schema` (افتراضياً TOOL_LOOP_SCHEMA)؛ إذا طلب
    الموديل أداة ينفّذها ويعيد التوليد بنتيجتها، حتى رد نهائي أو بلوغ
    `max_rounds`. يرجع دائماً dict فيه على الأقل "final_answer" — الحقول
    الإضافية (مثل order_ready) تصل كما رجّعها الموديل بردّه الأخير."""
    working_messages = list(messages)
    guided_json = schema or TOOL_LOOP_SCHEMA

    for _ in range(max_rounds):
        prompt = llm_engine.render_prompt(working_messages)
        text = await llm_engine.generate_full(
            prompt,
            max_tokens=max_tokens,
            temperature=temperature,
            guided_json=guided_json,
        )

        data = _parse_action(text)
        if data is None:
            logger.warning("رد الموديل ما طابق مخطط tool_call/final_answer: %r", text[:200])
            return {"final_answer": _EXHAUSTED_FALLBACK}

        if data.get("action") == "final_answer":
            data["final_answer"] = (data.get("final_answer") or "").strip() or _EXHAUSTED_FALLBACK
            return data

        call = data.get("tool_call") or {}
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

        working_messages.append({
            "role": "assistant",
            "content": json.dumps(data, ensure_ascii=False),
        })
        working_messages.append({
            "role": "user",
            "content": (
                f"[نتيجة الأداة {tool_name}]: {json.dumps(tool_result, ensure_ascii=False)}\n"
                "تابع ردك بالاعتماد على هذه النتيجة — أرجع action=final_answer إذا "
                "اكتملت المعلومة، أو tool_call ثانية إذا تحتاج معلومة إضافية."
            ),
        })

    logger.warning("نفدت جولات الأدوات (%s) بلا رد نهائي", max_rounds)
    return {"final_answer": _EXHAUSTED_FALLBACK}
