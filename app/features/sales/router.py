# -*- coding: utf-8 -*-
"""وكيل المبيعات: POST /sales/chat و /sales/chat/stream.

عندما يقرر الوكيل إن العميل جاهز للشراء (يخرج [ORDER_READY])، نشغّل تلقائياً
جولة توليد ثانية بـ system prompt مختلف (ORDER_EXTRACTION_SYSTEM_PROMPT) لاستخراج
JSON، ونحسب الأسعار/المجموع من الكتالوج الحقيقي بدل الثقة بأرقام الموديل.
"""

import json
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import sessions
from app.config import settings
from app.context_blocks import products_context_block
from app.engine import llm_engine
from app.features.sales.prompts import (
    ORDER_READY_MARKER,
    build_order_extraction_prompt,
    build_sales_prompt,
)
from app.features.sales.service import resolve_order
from app.guards import check_numbers, check_topics
from app.order_schema import OrderConfirmation, OrderExtraction, parse_order_extraction
from app.products import product_repository
from app.rag import search as search_words

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sales", tags=["sales"])

_SESSION_PREFIX = "sales:"

_PURCHASE_KEYWORDS = ["اشتريها", "اشتريه", "خلص اشتري", "احجزلي", "ابيها", "أبيها", "موافق", "زبطت", "خذلي"]

# رد بديل حتمي عندما يتدخل درع المواضيع (app/guards.py: check_topics) — الموديل
# ادّعى معلومة عن موضوع (ضمان/تركيب/تقسيط...) غير مغطّى بمعلومات المنتج
# المسترجَعة، فنستبدل الرد بإحالة صريحة بدل ترك هلوسة توصل للعميل.
SAFE_TOPIC_REPLY = "خليني أتأكد من هذي المعلومة وأرد عليك، حتى ما أگلك شي غلط."


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
        return f"خليني أدقّقلك السعر حتى ما أغلطلك — اللي أگدر أأكده هسه: {lines}."
    # ماكو منتج مطابق أصلاً بالكتالوج — الرد الطبيعي رفض توفّر (الموديل
    # غالباً اخترع منتجاً وسعراً سوية)، مو كلام عن دقة السعر.
    return "والله هذا ماكو عدنا هسه، بس أتأكدلك إذا راح يتوفر وأرد عليك."


def _fallback_order_ready(message: str) -> bool:
    return any(kw in message for kw in _PURCHASE_KEYWORDS)


def _fallback_extraction(message: str) -> OrderExtraction:
    return OrderExtraction(items=[{"product_name": message, "quantity": 1}])


async def _maybe_build_order(session_key: str, rag_words: List[dict]) -> Optional[OrderConfirmation]:
    history = sessions.get(session_key)
    if llm_engine.ready:
        extraction_messages = build_order_extraction_prompt(history, rag_words)
        extraction_prompt = llm_engine.render_prompt(extraction_messages)
        schema = OrderExtraction.model_json_schema()
        raw = await llm_engine.generate_full(
            extraction_prompt, max_tokens=256, temperature=0.0, guided_json=schema
        )
        extraction = parse_order_extraction(raw)
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
        reference_text = products_context_block(rag_products)
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
                answer = _safe_price_answer(rag_products)

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
            reference_text = products_context_block(rag_products)
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
                    answer = _safe_price_answer(rag_products)
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
