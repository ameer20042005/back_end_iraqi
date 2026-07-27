# -*- coding: utf-8 -*-
"""دعم العملاء: POST /support/chat — تتبع حالة الطلب برقم الطلب أو الهاتف."""

import re
import uuid
from typing import Optional

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

# استعلامات جماعية عن دفتر الطلبات («شنو الطلبات قيد التوصيل؟»، «كم طلب
# عدكم؟») — سؤال عن طلبات **غير** المتحدث، بلا أي معرّف يخصّه. هذي ما تُفوَّض
# للموديل أبداً: مرصود بالاختبار الفعلي إنه يخترع معرّفات طلبات ويسند لها حالات
# ما تطابق `orders.json` (گال ORD-1002 و ORD-1003 «قيد التوصيل» بينما الأولى
# «تم التسليم» والثانية «قيد التجهيز»). دفتر الطلبات كله بيانات عملاء آخرين،
# فالرد الصحيح رفض مهذّب يطلب معرّف المتحدث نفسه.
_BULK_QUERY_WORDS = (
    "الطلبات", "طلبات", "كل الطلبات", "جميع الطلبات", "قائمه الطلبات",
    "لائحه الطلبات", "الزباين", "الزبائن", "العملاء", "العمله",
)
# طلب صريح لبيانات اتصال — حتى لو مقترن بمعرّف طلب. رقم الهاتف بسجل الطلب
# بيانات شخصية، والدعم ما يقرأها للسائل: هو أصلاً صاحب الرقم إذا الطلب طلبه.
_CONTACT_REQUEST_WORDS = (
    "ارقام الهواتف", "ارقام الهاتف", "رقم الهاتف", "ارقام هواتف", "الارقام",
    "رقم الموبايل", "ارقام الموبايل", "رقم التلفون", "ارقام التلفون",
    "عناوين", "العناوين", "عنوان الزبون", "معلومات الزبون", "بيانات الزبون",
)

_BULK_REFUSAL = (
    "معذرة، ما أگدر أعطي معلومات عن طلبات زباين آخرين — هذي بيانات خاصة. "
    "إذا تريد تعرف حالة طلبك إنته، عطيني رقم الطلب (مثل ORD-1001) أو رقم "
    "هاتفك وأتابعلك."
)
_CONTACT_REFUSAL = (
    "معذرة، ما أگدر أنطي أرقام هواتف ولا بيانات اتصال — هذي معلومات خاصة "
    "بأصحابها. أگدر أساعدك بحالة طلبك إذا تنطيني رقم الطلب أو رقم هاتفك."
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
    order_id = args.get("order_id")
    phone = args.get("phone")
    if order_id:
        order = await order_status_provider.get_by_order_id(str(order_id))
        return order or {"error": "ماكو طلب بهذا الرقم"}
    if phone:
        orders = await order_status_provider.search_by_phone(str(phone))
        return {"orders": orders} if orders else {"error": "ماكو طلبات بهذا الرقم"}
    return {"error": "لازم تزودني برقم الطلب أو رقم الهاتف"}


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


def _privacy_refusal(message: str) -> Optional[str]:
    """يرجع رد رفض جاهزاً إذا الرسالة تطلب بيانات ما تخصّ المتحدث.

    حالتان، وكلاهما لازم يُحسم هنا لا بالموديل:
      1. طلب بيانات اتصال (أرقام هواتف/عناوين) — يُرفض دائماً، حتى لو الرسالة
         فيها رقم طلب صحيح، لأن الجواب تسريب بيانات شخصية بأي حال.
      2. استعلام جماعي عن الطلبات بلا معرّف يخصّ المتحدث — يُرفض ويُطلب معرّف.
         وجود معرّف بالرسالة يعني السؤال عن طلب المتحدث نفسه، فيمر عادي."""
    normalized = normalize(message)
    if any(w in normalized for w in _CONTACT_REQUEST_WORDS):
        return _CONTACT_REFUSAL
    has_identifier = extract_order_id(message) or extract_phone(message)
    if not has_identifier and any(w in normalized for w in _BULK_QUERY_WORDS):
        return _BULK_REFUSAL
    return None


async def _deterministic_status_answer(message: str) -> Optional[str]:
    """توجيه حتمي لطلبات التتبع: إذا الرسالة فيها رقم طلب أو هاتف، نستعلم
    من المصدر مباشرة ونبني الرد من البيانات الحقيقية — بدون تفويض القرار
    للموديل. السبب (مرصود بالاختبار الفعلي): الموديل الحالي لا يستدعي
    [TOOL_CALL] بموثوقية ويخترع حالات طلب من خياله («قيد التجهيز يوصل خلال
    يوم» لطلب حالته الحقيقية «قيد التوصيل خلال يومين») — hallucination خطير
    بميزة دعم. يرجع None إذا الرسالة ما فيها معرّف، فتذهب لمسار الموديل+الأدوات."""
    refusal = _privacy_refusal(message)
    if refusal:
        return refusal

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

    return None


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
