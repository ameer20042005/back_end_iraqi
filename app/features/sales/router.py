# -*- coding: utf-8 -*-
"""وكيل المبيعات: POST /sales/chat و /sales/chat/stream.

عندما يكتمل الطلب — يخرج الوكيل [ORDER_READY]، أو يُكتشف الاكتمال من محتوى
المحادثة عند نسيان العلامة (_order_complete_by_content) — نشغّل تلقائياً جولة
توليد ثانية ببرومت plane.md نفسه لاستخراج JSON، ونحسب الأسعار/المجموع من
الكتالوج الحقيقي بدل الثقة بأرقام الموديل.

صيغة الطلب موحّدة مع /orders/create حرفياً (مخطط plane.md عبر
app/order_extraction.py)، فما تختلف حسب مصدر الطلب.
"""

import json
import logging
import re
import uuid
from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import sessions
from app.config import settings
from app.context_blocks import products_context_block
from app.engine import llm_engine
from app.features.order_intake.prompts import build_order_intake_prompt
from app.features.sales.prompts import (
    ORDER_READY_MARKER,
    build_sales_prompt,
)
from app.features.sales.service import resolve_order
from app.guards import check_numbers, check_topics
from app.order_extraction import correct_location, state_code_for
from app.order_schema import (
    OrderConfirmation,
    OrderExtraction,
    PlaneOrderExtraction,
    parse_plane_extraction,
)
from app.products import product_repository
from app.rag import search_locations
from app.rag import search as search_words

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sales", tags=["sales"])

_SESSION_PREFIX = "sales:"

_PURCHASE_KEYWORDS = ["اشتريها", "اشتريه", "خلص اشتري", "احجزلي", "ابيها", "أبيها", "موافق", "زبطت", "خذلي"]

# تأكيد العميل النهائي بعد ملخّص الطلب («نعم ثبت»، «اي اكيد»).
_CONFIRM_KEYWORDS = [
    "ثبت", "ثبتها", "ثبته", "اي اكيد", "اي أكيد", "اكيد", "أكيد", "نعم",
    "موافق", "زبطت", "تمام", "اي زين", "اوكي", "اوك", "خلص",
]
# إقرار الوكيل إن الطلب انثبت فعلاً بنفس الرد («تم الطلب»، «سجلتلك الطلب»).
_ORDER_DONE_PHRASES = [
    "تم الطلب", "تم تثبيت", "ثبتلك الطلب", "سجلتلك الطلب", "تم تسجيل",
    "طلبك تم", "انثبت الطلب", "تم حجز", "حجزتلك",
]
_PHONE_IN_TEXT_RE = re.compile(r"07\d{9}")


def _order_complete_by_content(history: List[dict], user_message: str, answer: str) -> bool:
    """كشف احتياطي لاكتمال الطلب من محتوى المحادثة نفسها.

    ليش موجود: إصدار الطلب كان معلّقاً حصراً على أن يكتب الموديل سطر
    [ORDER_READY] حرفياً. موديل 4B مضبوط على اللهجة العراقية ينسى العلامة
    التقنية بسهولة ويختم بكلام طبيعي («خوش، تم الطلب») — فتكتمل المحادثة
    كلها وما يصدر أي JSON، وهذا اللي صار بالإنتاج فعلاً.

    الشرط هنا متحفّظ عمداً حتى ما يصدر طلب ناقص: لازم يجتمع (١) هاتف عراقي
    بالمحادثة، (٢) تأكيد صريح من العميل بهذي الرسالة، (٣) إقرار من الوكيل
    إن الطلب انثبت. العلامة تبقى المسار الأساسي وهذا شبكة أمان تحتها.
    """
    conversation = " ".join(m.get("content", "") for m in history) + " " + user_message
    if not _PHONE_IN_TEXT_RE.search(conversation):
        return False
    if not any(k in user_message for k in _CONFIRM_KEYWORDS):
        return False
    return any(p in answer for p in _ORDER_DONE_PHRASES)

# رد بديل حتمي عندما يتدخل درع المواضيع (app/guards.py: check_topics) — الموديل
# ادّعى معلومة عن موضوع (ضمان/تركيب/تقسيط...) غير مغطّى بمعلومات المنتج
# المسترجَعة، فنستبدل الرد بإحالة صريحة بدل ترك هلوسة توصل للعميل.
SAFE_TOPIC_REPLY = (
    "عذراً حبيبي، هذي النقطة تحديداً أحب أتأكد منها قبل ما أگلك شي، "
    "خليني أتحقق وأرجعلك بيها فوراً."
)


class SalesChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


class SalesChatResponse(BaseModel):
    session_id: str
    answer: str
    order: Optional[OrderConfirmation] = None
    sources: dict
    engine: str


def _fallback_sales_answer(message: str, rag_products: List[dict]) -> str:
    if rag_products:
        top = rag_products[0]
        return f"[وضع محلي بدون GPU] عندنا {top['name']} بسعر {top['price']} {top.get('currency', '')}."
    return f"[وضع محلي بدون GPU] ما لكيت منتج مطابق لـ: {message}"


def _safe_price_answer(rag_products: List[dict]) -> str:
    """رد بديل حتمي يُستخدم عندما يكتشف حارس الأرقام سعراً مختلَقاً برد
    الموديل — مبني حصراً من أسعار الكتالوج الحقيقية، فلا يصل أي رقم مختلَق
    للعميل أبداً."""
    if rag_products:
        lines = "، ".join(
            f"{p['name']} بـ{p['price']:,} {p.get('currency', 'IQD')}" for p in rag_products[:3]
        )
        return (
            "معليش حبيبي، حبيت أدقّقلك السعر حتى ما أغلط وياك — "
            f"اللي أگدر أأكده إلك هسه: {lines}."
        )
    # ماكو أي منتج معروف بهذي الجلسة. الرد القديم كان يرفض التوفّر («ماكو
    # عدنا») وهذا غلط فادح بسياق تثبيت طلب أو إعطاء عنوان — العميل يعطي
    # بياناته فيجيه رفض. الرد المحايد يصلح لكل السياقات: نأجّل الرقم بس، ولا
    # ندّعي نفي توفّر ما نعرفه.
    return (
        "عذراً حبيبي، خليني أتأكدلك من التفاصيل والسعر بالضبط وأرجعلك بيها "
        "فوراً حتى ما أگلك رقم غلط."
    )


def _fallback_order_ready(message: str) -> bool:
    return any(kw in message for kw in _PURCHASE_KEYWORDS)


def _fallback_extraction(message: str) -> OrderExtraction:
    return OrderExtraction(items=[{"product_name": message, "quantity": 1}])


async def _maybe_build_order(session_key: str, rag_words: List[dict]) -> Optional[OrderConfirmation]:
    """يبني الطلب النهائي من المحادثة بمخطط plane.md — نفس مخطط
    /orders/create حرفياً، حتى يطلع الطلب بصيغة وحدة مهما كان مصدره
    (محادثة مبيعات، أو رسالة/صوت/صورة خام). كان هذا المسار يستعمل مخططاً
    مختلفاً (customer_name/items) فيرجع للعميل شكل JSON ثاني."""
    history = sessions.get(session_key)
    if llm_engine.ready:
        conversation = "\n".join(
            f"{'العميل' if m['role'] == 'user' else 'الوكيل'}: {m['content']}"
            for m in history
        )
        rag_locations = search_locations(conversation)
        extraction_messages = build_order_intake_prompt(
            conversation, rag_words, rag_locations
        )
        schema = PlaneOrderExtraction.model_json_schema()
        raw = await llm_engine.generate_full(
            llm_engine.render_prompt(extraction_messages),
            max_tokens=384, temperature=0.0, guided_json=schema,
        )
        plane = parse_plane_extraction(raw)
        if plane is None:
            logger.warning(
                "استخراج طلب المبيعات فشل — الناتج الخام: %r (session=%s)",
                raw[:500], session_key,
            )
            last_user = next(
                (m["content"] for m in reversed(history) if m["role"] == "user"), ""
            )
            extraction = _fallback_extraction(last_user)
        else:
            extraction = correct_location(plane).to_order_extraction()
            extraction.state_code = state_code_for(extraction.customer_city)
    else:
        last_user = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        extraction = _fallback_extraction(last_user)
    return await resolve_order(extraction)


@router.post("/chat", response_model=SalesChatResponse)
async def sales_chat(req: SalesChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)
    rag_words = search_words(req.message, top_k=settings.rag_top_k)
    rag_products = product_repository.search(req.message, top_k=5)
    sessions.remember_products(key, rag_products)
    # مرجع الدروع = منتجات الجلسة كلها لا هذا الدور وحده (انظر
    # sessions.remember_products): آخر رسالة بأي طلب هي بيانات العميل بلا اسم
    # منتج، فالاسترجاع يرجع فارغاً ويُحجب سعر ذُكر قبل دورين بغير حق.
    known_products = sessions.known_products(key)
    messages = build_sales_prompt(history, req.message, rag_words, rag_products)

    if llm_engine.ready:
        prompt = llm_engine.render_prompt(messages)
        result_holder: dict = {}
        answer = await llm_engine.generate_full(
            prompt,
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            stop=[ORDER_READY_MARKER],
            result_holder=result_holder,
        )
        order_ready = result_holder.get("stop_reason") == ORDER_READY_MARKER
        engine_name = "vllm"
    else:
        answer = _fallback_sales_answer(req.message, rag_products)
        order_ready = _fallback_order_ready(req.message)
        engine_name = "fallback"

    # الدروع ثنائية الاتجاه — إلغاء فعلي مو تسجيل فقط (نفس تسلسل خلية
    # الاستدلال بالنوتبوك: المواضيع أولاً لأنها تفهم السياق، الأرقام ثانياً).
    if engine_name == "vllm":
        reference_text = products_context_block(known_products)
        reason = check_topics(answer, req.message, reference_text)
        if reason:
            logger.warning(
                "درع المواضيع تدخّل برد المبيعات: %s (session=%s) — الرد الأصلي: %r",
                reason, session_id, answer[:300],
            )
            answer = SAFE_TOPIC_REPLY
        else:
            bad_numbers = check_numbers(answer, reference_text)
            if bad_numbers:
                logger.warning(
                    "أرقام مختلَقة برد المبيعات أُلغيت واستُبدل الرد: %s (session=%s) — الرد الأصلي: %r",
                    bad_numbers, session_id, answer[:300],
                )
                answer = _safe_price_answer(known_products)

    # شبكة أمان تحت العلامة: الموديل ينسى [ORDER_READY] ويختم بكلام طبيعي
    # («خوش، تم الطلب») فتضيع الطلبات المكتملة — انظر _order_complete_by_content.
    if not order_ready and engine_name == "vllm" and _order_complete_by_content(
        history, req.message, answer
    ):
        logger.info(
            "الطلب اكتمل بالمحتوى بدون علامة %s (session=%s)", ORDER_READY_MARKER, session_id
        )
        order_ready = True

    sessions.append(key, "user", req.message)
    sessions.append(key, "assistant", answer)

    order = await _maybe_build_order(key, rag_words) if order_ready else None

    return SalesChatResponse(
        session_id=session_id,
        answer=answer,
        order=order,
        sources={"words": rag_words, "products": rag_products},
        engine=engine_name,
    )


@router.post("/chat/stream")
async def sales_chat_stream(req: SalesChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)
    rag_words = search_words(req.message, top_k=settings.rag_top_k)
    rag_products = product_repository.search(req.message, top_k=5)
    sessions.remember_products(key, rag_products)
    known_products = sessions.known_products(key)
    messages = build_sales_prompt(history, req.message, rag_words, rag_products)

    async def event_source():
        # نجمّع الرد كاملاً قبل بثّه (مو delta بـ delta) عمداً: حارس الأرقام
        # لازم يفحص الرد كاملاً قبل ما يوصل أي جزء منه للعميل — رقم مختلَق
        # مبثوث حياً ما ينسحب. الردود قصيرة أصلاً (64 توكن) فالتأخير مقبول.
        collected = []
        order_ready = False
        if llm_engine.ready:
            prompt = llm_engine.render_prompt(messages)
            result_holder: dict = {}
            async for delta in llm_engine.generate_stream(
                prompt,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                stop=[ORDER_READY_MARKER],
                result_holder=result_holder,
            ):
                collected.append(delta)
            order_ready = result_holder.get("stop_reason") == ORDER_READY_MARKER
            engine_name = "vllm"
            answer = "".join(collected)
            reference_text = products_context_block(known_products)
            reason = check_topics(answer, req.message, reference_text)
            if reason:
                logger.warning(
                    "درع المواضيع تدخّل برد المبيعات (stream): %s (session=%s) — الرد الأصلي: %r",
                    reason, session_id, answer[:300],
                )
                answer = SAFE_TOPIC_REPLY
            else:
                bad_numbers = check_numbers(answer, reference_text)
                if bad_numbers:
                    logger.warning(
                        "أرقام مختلَقة برد المبيعات (stream) أُلغيت واستُبدل الرد: %s (session=%s) — الرد الأصلي: %r",
                        bad_numbers, session_id, answer[:300],
                    )
                    answer = _safe_price_answer(known_products)
            # نفس شبكة الأمان بمسار البث (انظر _order_complete_by_content).
            if not order_ready and _order_complete_by_content(
                history, req.message, answer
            ):
                logger.info(
                    "الطلب اكتمل بالمحتوى بدون علامة %s (stream, session=%s)",
                    ORDER_READY_MARKER, session_id,
                )
                order_ready = True
        else:
            answer = _fallback_sales_answer(req.message, rag_products)
            order_ready = _fallback_order_ready(req.message)
            engine_name = "fallback"

        yield f"data: {json.dumps({'delta': answer}, ensure_ascii=False)}\n\n"

        sessions.append(key, "user", req.message)
        sessions.append(key, "assistant", answer)

        order = await _maybe_build_order(key, rag_words) if order_ready else None

        yield "data: " + json.dumps(
            {
                "done": True,
                "session_id": session_id,
                "sources": {"words": rag_words, "products": rag_products},
                "order": order.model_dump() if order else None,
            },
            ensure_ascii=False,
        ) + "\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")
