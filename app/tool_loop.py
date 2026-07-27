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
import logging
import re
from typing import Awaitable, Callable, Dict, List, Optional

from app.engine import llm_engine

logger = logging.getLogger(__name__)

# الصيغة المتوقَّعة: [TOOL_CALL] بآخر النص (الـ stop string يقطع التوليد عنده).
_TOOL_CALL_TAIL = re.compile(r"\[TOOL_CALL\]\s*(\{.*\})\s*$", re.DOTALL)

# صيغ منحرفة يخرجها الموديل فعلياً حين ما يلتزم حرفياً بالتعليمات. كل واحدة
# كانت تُسقِط الاستدعاء بصمت فيتسرّب نص خام («[TOOL_CALL]{...}») للعميل، أو
# أسوأ: يقرر الموديل يجاوب من خياله لأن الأداة «ما ردّت عليه».
# ملاحظة على `\{.*\}`: جشع عمداً حتى يبلع الأقواس المتداخلة بـ args.
_TOOL_CALL_ANYWHERE = re.compile(
    r"\[TOOL_CALL\]\s*(\{.*\})\s*(?:\[/TOOL_CALL\])?", re.DOTALL
)
# بلا وسوم إطلاقاً: JSON عارٍ فيه مفتاح "tool" — الموديل «يتذكّر» البنية
# وينسى الوسوم، وهي أكثر الانحرافات شيوعاً بالنماذج غير المدرَّبة على النمط.
# القوس الأخير جشع (`.*\}`) عمداً: `args` كائن متداخل، وأي صيغة غير جشعة
# تتوقف عند أول `}` داخلي فتنتج JSON مبتوراً لا يُفكَّك.
_BARE_JSON = re.compile(r'(\{[^{}]*"tool"\s*:\s*"[^"]+".*\})', re.DOTALL)

# رد احتياطي حين تنفد الجولات بلا إجابة نهائية. بديله السابق كان إرجاع آخر نص
# مولَّد — أي غالباً استدعاء أداة نصف مكتوب يوصل العميل كما هو.
_EXHAUSTED_FALLBACK = (
    "معذرة، صار عندي تعقيد بجلب المعلومة. عطيني رقم الطلب (مثل ORD-1001) أو "
    "رقم هاتفك وأتابعلك مباشرة."
)

ToolFunc = Callable[[dict], Awaitable[dict]]


def _extract_tool_call(text: str):
    """يستخرج (استدعاء الأداة، النص المرئي قبله) من رد الموديل، أو (None, "").

    يجرّب ثلاث صيغ بترتيب الصرامة: الصيغة القياسية بآخر النص، ثم الصيغة
    الموسومة أينما وردت (حتى لو تبعها كلام)، ثم JSON عارٍ بمفتاح "tool".
    التساهل هنا مقصود ومحدود: البديل عن قراءة استدعاء منحرف ليس «رفضه بأمان»
    بل تسريبه نصاً خاماً للعميل أو دفع الموديل للاختراع."""
    for pattern in (_TOOL_CALL_TAIL, _TOOL_CALL_ANYWHERE, _BARE_JSON):
        match = pattern.search(text)
        if not match:
            continue
        try:
            call = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(call, dict) and call.get("tool"):
            return call, text[: match.start()].strip()
    return None, ""


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

        call, visible_text = _extract_tool_call(text)

        # ما بيه استدعاء أداة = رد نهائي للعميل. نفحص النص نفسه لا `stop_reason`
        # وحده: الموديل أحياناً يكتب الاستدعاء بلا الوسم الختامي فما يُفعَّل الـ
        # stop string، وكان الرد يُسلَّم للعميل بوسومه الخام.
        if call is None:
            if "[TOOL_CALL]" in text:
                # استدعاء مشوَّه تعذّر تفكيكه — نحجب النص الخام ولا نسلّمه.
                logger.warning("استدعاء أداة مشوَّه تعذّر تفكيكه: %r", text[:200])
                return _EXHAUSTED_FALLBACK
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

        working_messages.append({"role": "assistant", "content": visible_text or "..."})
        working_messages.append({
            "role": "user",
            "content": (
                f"[نتيجة الأداة {tool_name}]: {json.dumps(tool_result, ensure_ascii=False)}\n"
                "تابع ردك للعميل بالاعتماد على هذه النتيجة."
            ),
        })

    # نفدت الجولات والموديل ما وصل لرد نهائي. الإرجاع السابق (`text.strip()`)
    # كان يسلّم العميل آخر نص مولَّد — وهو بهذي النقطة بالذات استدعاء أداة
    # (لأن كل جولة انتهت باستدعاء، وإلا رجعنا مبكراً). أي: النص الوحيد المضمون
    # إنه **مو** رد للعميل هو بالضبط اللي كان يُرسَل له.
    logger.warning("نفدت جولات الأدوات (%s) بلا رد نهائي", max_rounds)
    return _EXHAUSTED_FALLBACK
