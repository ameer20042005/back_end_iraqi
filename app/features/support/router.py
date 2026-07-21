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
from app.tool_loop import run_with_tools
from app.tools.web_search import web_search_tool

router = APIRouter(prefix="/support", tags=["support"])

_SESSION_PREFIX = "support:"
_ORDER_ID_RE = re.compile(r"ORD-\d+", re.IGNORECASE)
_PHONE_RE = re.compile(r"07\d{9}")


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


async def _deterministic_status_answer(message: str) -> Optional[str]:
    """توجيه حتمي لطلبات التتبع: إذا الرسالة فيها رقم طلب أو هاتف، نستعلم
    من المصدر مباشرة ونبني الرد من البيانات الحقيقية — بدون تفويض القرار
    للموديل. السبب (مرصود بالاختبار الفعلي): الموديل الحالي لا يستدعي
    [TOOL_CALL] بموثوقية ويخترع حالات طلب من خياله («قيد التجهيز يوصل خلال
    يوم» لطلب حالته الحقيقية «قيد التوصيل خلال يومين») — hallucination خطير
    بميزة دعم. يرجع None إذا الرسالة ما فيها معرّف، فتذهب لمسار الموديل+الأدوات."""
    order_match = _ORDER_ID_RE.search(message)
    if order_match:
        order = await order_status_provider.get_by_order_id(order_match.group().upper())
        if order:
            return _format_order_reply(order)
        return "والله ماكو طلب بهذا الرقم عدنا — دقّق الرقم وگلي مرة ثانية."

    phone_match = _PHONE_RE.search(message)
    if phone_match:
        orders = await order_status_provider.search_by_phone(phone_match.group())
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
