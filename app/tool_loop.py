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
from typing import AsyncGenerator, Awaitable, Callable, Dict, List, Optional, Tuple

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

# مخطط "قرار" مصغّر — بدون final_answer: يُستخدم فقط بمسار البث
# (run_decision_rounds أدناه) لجولات استدعاء الأدوات، حيث القرار الوحيد
# المطلوب من الموديل هو "أحتاج أداة ثانية أم انتهيت؟" + أي حقول إضافية
# (order_ready). فصل هذا عن final_answer يسمح بجولة توليد حرة لاحقة (بلا
# guided_json) تُبث توكن-بتوكن فعلياً — انظر stream_final_answer.
_DECISION_PROPERTIES = {
    "action": {"type": "string", "enum": ["tool_call", "done"]},
    "tool_call": {
        "type": "object",
        "properties": {
            "tool": {"type": "string"},
            "args": {"type": "object"},
        },
        "required": ["tool"],
    },
}


def build_decision_schema(
    extra_properties: Optional[dict] = None, extra_required: Optional[List[str]] = None
) -> dict:
    """يبني مخطط القرار المصغّر، بحقول إضافية اختيارية (مثل order_ready)."""
    return {
        "type": "object",
        "properties": {**_DECISION_PROPERTIES, **(extra_properties or {})},
        "required": ["action"] + (extra_required or []),
    }


DECISION_SCHEMA = build_decision_schema()

# رد احتياطي حين تنفد الجولات بلا إجابة نهائية، أو يجي نص فارغ من جولة بث
# حرة (stream_final_answer) — معرَّض للاستيراد من الراوترات لنفس السبب.
EXHAUSTED_FALLBACK = (
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
    # سجل كل استدعاء أداة بهذه الجولة — يُرجَع مع النتيجة النهائية حتى تقدر
    # الواجهة (static/index.html) تعرضه بشكل مرئي بدل ما يبقى داخلياً فقط.
    # لا يُستخدم بأي منطق قرار هنا، مجرد شفافية للمتصل.
    tool_calls_log: List[dict] = []

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
            return {"final_answer": EXHAUSTED_FALLBACK, "tool_calls": tool_calls_log}

        if data.get("action") == "final_answer":
            data["final_answer"] = (data.get("final_answer") or "").strip() or EXHAUSTED_FALLBACK
            data["tool_calls"] = tool_calls_log
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
        tool_calls_log.append({"tool": tool_name, "args": args, "result": tool_result})

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
    return {"final_answer": EXHAUSTED_FALLBACK, "tool_calls": tool_calls_log}


async def run_decision_rounds(
    messages: List[Dict[str, str]],
    tools: Dict[str, ToolFunc],
    max_rounds: int = 3,
    schema: Optional[dict] = None,
) -> Tuple[List[Dict[str, str]], dict, List[dict]]:
    """جولات قرار الأدوات فقط (بلا نص نهائي) — مقيَّدة بـ DECISION_SCHEMA
    (أو `schema` مخصص بنفس القاعدة). تُنفّذ استدعاءات الأداة كما
    run_with_tools تماماً، لكن بمجرد ما يقرر الموديل action="done" تتوقف
    فوراً بلا توليد أي نص — النص الفعلي يتولاه stream_final_answer بجولة
    منفصلة غير مقيَّدة قابلة للبث الحقيقي.

    ترجع (working_messages, decision_data, tool_calls_log): working_messages
    جاهزة تُمرَّر مباشرة لـ stream_final_answer، وdecision_data يحمل أي حقول
    إضافية (مثل order_ready) وصلت بآخر قرار."""
    working_messages = list(messages)
    guided_json = schema or DECISION_SCHEMA
    tool_calls_log: List[dict] = []

    for _ in range(max_rounds):
        prompt = llm_engine.render_prompt(working_messages)
        # 128 يكفي بوفرة لمخطط القرار المصغّر (action + tool_call صغير أو
        # order_ready) — أقصر بكثير من ردود المبيعات الكاملة، فيقلّل زمن هذي
        # الجولة قبل ما توصل جولة البث النهائي.
        text = await llm_engine.generate_full(prompt, max_tokens=128, guided_json=guided_json)

        data = _parse_action(text)
        if data is None:
            logger.warning("رد قرار الأدوات ما طابق المخطط: %r", text[:200])
            return working_messages, {}, tool_calls_log

        if data.get("action") != "tool_call":
            return working_messages, data, tool_calls_log

        call = data.get("tool_call") or {}
        tool_name = call.get("tool")
        args = call.get("args") or {}
        tool_func = tools.get(tool_name)
        if tool_func is None:
            tool_result = {"error": f"أداة غير معروفة: {tool_name}"}
        else:
            try:
                tool_result = await tool_func(args)
            except Exception as exc:
                tool_result = {"error": str(exc)}
        tool_calls_log.append({"tool": tool_name, "args": args, "result": tool_result})

        working_messages.append({
            "role": "assistant",
            "content": json.dumps(data, ensure_ascii=False),
        })
        working_messages.append({
            "role": "user",
            "content": (
                f"[نتيجة الأداة {tool_name}]: {json.dumps(tool_result, ensure_ascii=False)}\n"
                "تابع — أرجع action=\"done\" إذا اكتملت المعلومة لكتابة الرد "
                "النهائي، أو tool_call ثانية إذا تحتاج معلومة إضافية."
            ),
        })

    logger.warning("نفدت جولات قرار الأدوات (%s) بلا انتهاء", max_rounds)
    return working_messages, {}, tool_calls_log


async def stream_final_answer(
    working_messages: List[Dict[str, str]],
    max_tokens: Optional[int] = None,
) -> AsyncGenerator[str, None]:
    """يبثّ النص النهائي توكن-بتوكن فعلياً — تُستدعى بعد run_decision_rounds
    بمجرد ما ينتهي الموديل من الأدوات. توليد حر (بلا guided_json) بتعليمة
    صريحة تكتب النص مباشرة لا JSON، عبر app.engine.stream_chat_completion.

    مو مناسبة لتوليد order_ready أو أي حقل بنيوي آخر — هذي مسؤولية
    run_decision_rounds قبلها؛ هنا فقط الكلام الموجَّه للعميل."""
    final_prompt = list(working_messages) + [{
        "role": "user",
        "content": (
            "اكتب ردك النهائي الآن كنص عادي مباشر باللهجة العراقية — بدون "
            "JSON ولا أقواس ولا حقول، فقط الجملة اللي بيها الجواب للعميل."
        ),
    }]
    async for delta in llm_engine.stream_chat_completion(
        llm_engine.render_prompt(final_prompt),
        max_tokens=max_tokens or 512,
    ):
        yield delta
