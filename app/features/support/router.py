# -*- coding: utf-8 -*-
"""دعم العملاء: POST /support/chat — تتبع حالة الطلب برقم الطلب أو الهاتف."""

import re
import uuid
from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel

from app import sessions
from app.config import settings
from app.engine import llm_engine
from app.features.support.prompts import build_support_prompt
from app.order_gateway import order_status_provider
from app.rag import search as search_words
from app.text_norm import normalize
from app.tool_loop import run_with_tools
from app.tools.web_search import web_search_tool

router = APIRouter(prefix="/support", tags=["support"])

_SESSION_PREFIX = "support:"
_ORDER_ID_RE = re.compile(r"ORD[\s\-_]*(\d+)", re.IGNORECASE)

# حالات الطلب المعروفة بالنظام، وكل صيغة يكتبها بيها الموظف. البوت **داخلي
# للموظفين**، فسؤال «شنو الطلبات قيد التوصيل؟» استعلام تشغيلي مشروع يُجاب من
# المصدر مباشرة — مو طلب بيانات زبون غريب.
#
# ليش حتمي مو للموديل: الموديل مرصود إنه يخترع معرّفات طلبات ويسند لها حالات
# ما تطابق orders.json (گال ORD-1002 و ORD-1003 «قيد التوصيل» بينما حالتهما
# الحقيقية «تم التسليم» و«قيد التجهيز»). قائمة طلبات مخترَعة بميزة تتبع تشغيلي
# أسوأ من لا جواب: الموظف يتصرف على أساسها.
_STATUS_ALIASES = {
    "قيد التوصيل": ("قيد التوصيل", "التوصيل", "بالطريق", "طالعه", "مشحونه", "قيد الشحن"),
    "قيد التجهيز": ("قيد التجهيز", "التجهيز", "تجهيز", "قيد التحضير", "بالمخزن"),
    "تم التسليم": ("تم التسليم", "التسليم", "مسلمه", "واصله", "وصلت", "منتهيه"),
    "ملغي": ("ملغي", "ملغيه", "الملغيه", "الغيت", "مرفوضه"),
}

# أسئلة الجرد العام («كل الطلبات»، «كم طلب عدنا؟») — بلا حالة محددة.
_LIST_ALL_WORDS = (
    "كل الطلبات", "جميع الطلبات", "كافه الطلبات", "قائمه الطلبات",
    "لائحه الطلبات", "كم طلب", "عدد الطلبات", "الطلبات كلها",
)

# طلب صريح لبيانات الاتصال — مشروع هنا: الموظف يحتاج رقم الزبون حتى يتصل بيه.
_CONTACT_REQUEST_WORDS = (
    "ارقام الهواتف", "ارقام الهاتف", "رقم الهاتف", "ارقام هواتف",
    "رقم الموبايل", "ارقام الموبايل", "رقم التلفون", "ارقام التلفون",
    "الهواتف", "هواتف",
)

# أرقام الهواتف العراقية: 07XXXXXXXXX (11 خانة). الزبون يكتبها بصيغ كثيرة —
# بأرقام عربية-هندية، بفواصل/شرطات، بمقدمة دولية (+964 / 00964 / 964) اللي
# تستبدل الصفر الأول. نطبّع أولاً بالطبقة ١ (أرقام لاتينية) ثم نلتقط الصيغ.
_PHONE_RE = re.compile(
    r"(?:(?:\+|00)?964[\s\-]*)?0?7[\s\-]*(\d[\s\-]*){9}"
)
_NON_DIGITS_RE = re.compile(r"\D")


class SupportChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


class SupportChatResponse(BaseModel):
    session_id: str
    answer: str
    engine: str


async def _get_order_status_tool(args: dict) -> dict:
    """أداة الموديل. تدعم أربعة استعلامات — البوت داخلي فكل البيانات متاحة.

    الاستعلام بالحالة والجرد كانا ناقصين، وهذا هو السبب الجذري لهلوسة الموديل:
    كان مأموراً «لا تجاوب من عندك» بينما الأداة ترفض كل صيغة يسألها بالحالة،
    فما بقي أمامه إلا الاختراع."""
    order_id = args.get("order_id")
    phone = args.get("phone")
    status = args.get("status")
    if order_id:
        order = await order_status_provider.get_by_order_id(str(order_id))
        return order or {"error": "ماكو طلب بهذا الرقم"}
    if phone:
        orders = await order_status_provider.search_by_phone(str(phone))
        return {"orders": orders} if orders else {"error": "ماكو طلبات بهذا الرقم"}
    if status:
        orders = await order_status_provider.search_by_status(str(status))
        return {"orders": orders} if orders else {"error": f"ماكو طلبات بحالة {status}"}
    if args.get("all"):
        return {"orders": await order_status_provider.list_all()}
    return {"error": "لازم تزودني برقم الطلب أو رقم الهاتف أو الحالة"}


def _format_order_reply(order: dict) -> str:
    """يبني رداً عراقياً حتمياً من بيانات الطلب الحقيقية — بدون موديل."""
    items = "، ".join(
        f"{it['product_name']} ×{it.get('quantity', 1)}" for it in order.get("items", [])
    )
    reply = f"هلا بيك، طلبك {order['order_id']} حالته: {order['status']}"
    if items:
        reply += f" ({items})"
    if order.get("eta"):
        reply += f"، والوصول المتوقع {order['eta']}"
    return reply + "."


def extract_order_id(message: str) -> Optional[str]:
    """يستخرج معرّف الطلب بصيغته القياسية ORD-#### من نص الزبون الخام.

    يقبل «ord 1001» و«ORD_1001» و«ORD-١٠٠١» — التطبيع يوحّدها كلها."""
    match = _ORDER_ID_RE.search(normalize(message, keep_punctuation=True))
    return f"ORD-{match.group(1)}" if match else None


def extract_phone(message: str) -> Optional[str]:
    """يستخرج رقم هاتف عراقي بصيغته القياسية 07XXXXXXXXX من نص الزبون الخام.

    يقبل الأرقام العربية-الهندية، والفواصل والشرطات داخل الرقم، والمقدمة
    الدولية (+964 / 00964 / 964) اللي تحلّ محل الصفر. يرجع None إذا ما وُجد
    رقم بطول عراقي صحيح (11 خانة تبدأ بـ 07)."""
    normalized = normalize(message, keep_punctuation=True)
    match = _PHONE_RE.search(normalized)
    if not match:
        return None

    digits = _NON_DIGITS_RE.sub("", match.group())
    digits = digits.removeprefix("00").removeprefix("964")
    if not digits.startswith("0"):
        digits = "0" + digits
    return digits if len(digits) == 11 and digits.startswith("07") else None


def extract_status(message: str) -> Optional[str]:
    """يستخرج حالة الطلب المقصودة من سؤال الموظف، أو None.

    نطابق أطول اسم مستعار أولاً: «قيد التوصيل» تحتوي «التوصيل»، ولو طابقنا
    الأقصر أولاً كان صح بالصدفة هنا وغلط بحالات ثانية."""
    normalized = normalize(message)
    best: Optional[tuple] = None
    for canonical, aliases in _STATUS_ALIASES.items():
        for alias in aliases:
            alias_n = normalize(alias)
            if alias_n in normalized and (best is None or len(alias_n) > best[0]):
                best = (len(alias_n), canonical)
    return best[1] if best else None


def _format_order_line(order: dict) -> str:
    """سطر طلب واحد بقائمة — يشمل رقم هاتف الزبون لأن البوت داخلي والموظف
    يحتاجه حتى يتصل بيه."""
    items = "، ".join(
        f"{it['product_name']} ×{it.get('quantity', 1)}" for it in order.get("items", [])
    )
    line = f"{order['order_id']} — {items}" if items else str(order["order_id"])
    line += f" | الهاتف {order['phone']}"
    if order.get("eta"):
        line += f" | الوصول {order['eta']}"
    return line


def _format_order_list(orders: List[dict], heading: str) -> str:
    """قائمة طلبات مبنية حتمياً من المصدر — كل معرّف وحالة بيها من
    orders.json حرفياً، ما بيها ولا رقم من عند الموديل."""
    if not orders:
        return f"ماكو {heading} حالياً."
    lines = "\n".join("• " + _format_order_line(o) for o in orders)
    return f"{heading} ({len(orders)}):\n{lines}"


async def _bulk_query_answer(message: str) -> Optional[str]:
    """يجاوب أسئلة الموظف التشغيلية عن دفتر الطلبات — حتمياً من المصدر.

    البوت داخلي للشركة، فهذي استعلامات شغل مشروعة مو تسريب بيانات. تُحسم هنا
    لا بالموديل لأن الموديل يخترع معرّفات وحالات (انظر _STATUS_ALIASES)."""
    normalized = normalize(message)

    status = extract_status(message)
    if status:
        orders = await order_status_provider.search_by_status(status)
        return _format_order_list(orders, f"الطلبات {status}")

    if any(w in normalized for w in _LIST_ALL_WORDS):
        orders = await order_status_provider.list_all()
        return _format_order_list(orders, "كل الطلبات")

    # طلب أرقام هواتف بلا حالة محددة: نعطي كل الطلبات بأرقامها — القائمة
    # أصلاً تحمل الهاتف بكل سطر.
    if any(w in normalized for w in _CONTACT_REQUEST_WORDS):
        orders = await order_status_provider.list_all()
        return _format_order_list(orders, "أرقام هواتف الطلبات")

    return None


async def _deterministic_status_answer(message: str) -> Optional[str]:
    """توجيه حتمي لطلبات التتبع: إذا الرسالة فيها رقم طلب أو هاتف، نستعلم
    من المصدر مباشرة ونبني الرد من البيانات الحقيقية — بدون تفويض القرار
    للموديل. السبب (مرصود بالاختبار الفعلي): الموديل الحالي لا يستدعي
    [TOOL_CALL] بموثوقية ويخترع حالات طلب من خياله («قيد التجهيز يوصل خلال
    يوم» لطلب حالته الحقيقية «قيد التوصيل خلال يومين») — hallucination خطير
    بميزة دعم. يرجع None إذا الرسالة ما فيها معرّف، فتذهب لمسار الموديل+الأدوات."""
    order_id = extract_order_id(message)
    if order_id:
        order = await order_status_provider.get_by_order_id(order_id)
        if order:
            return _format_order_reply(order)
        return "والله ماكو طلب بهذا الرقم عدنا — دقّق الرقم وگلي مرة ثانية."

    phone = extract_phone(message)
    if phone:
        orders = await order_status_provider.search_by_phone(phone)
        if orders:
            return "هلا بيك، هذي طلباتك: " + " | ".join(_format_order_reply(o) for o in orders)
        return "والله ماكو طلبات مسجلة بهذا الرقم — تأكد من الرقم وگلي."

    # ما بيها معرّف محدد: يمكن استعلام تشغيلي عن دفتر الطلبات (حالة/جرد).
    # يجي **بعد** المعرّفات عمداً: «حالة ORD-1001» لازم ترجع ذاك الطلب بالذات،
    # مو قائمة كل الطلبات اللي بنفس حالته.
    return await _bulk_query_answer(message)


async def _fallback_support_answer(message: str) -> str:
    """يُستخدم فقط إذا لم يكن الموديل متوفراً (محلياً بدون GPU)."""
    deterministic = await _deterministic_status_answer(message)
    if deterministic:
        return "[وضع محلي بدون GPU] " + deterministic
    return "[وضع محلي بدون GPU] عطيني رقم الطلب أو رقم الهاتف حتى اكدر اكَولك وين وصل."


@router.post("/chat", response_model=SupportChatResponse)
async def support_chat(req: SupportChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)
    rag_words = search_words(req.message, top_k=settings.rag_top_k)
    messages = build_support_prompt(history, req.message, rag_words)

    # طلبات التتبع (رقم طلب/هاتف بالرسالة) تُجاب حتمياً من المصدر مباشرة —
    # الموديل غير موثوق باستدعاء الأدوات ويخترع حالات طلب (انظر
    # _deterministic_status_answer). الموديل+الأدوات فقط للأسئلة العامة.
    deterministic = await _deterministic_status_answer(req.message)
    if deterministic is not None:
        answer = deterministic
        engine_name = "deterministic"
    elif llm_engine.ready:
        answer = await run_with_tools(messages, tools={
            "get_order_status": _get_order_status_tool,
            "web_search": web_search_tool,
        })
        engine_name = "vllm"
    else:
        answer = await _fallback_support_answer(req.message)
        engine_name = "fallback"

    sessions.append(key, "user", req.message)
    sessions.append(key, "assistant", answer)

    return SupportChatResponse(session_id=session_id, answer=answer, engine=engine_name)
