# -*- coding: utf-8 -*-
"""إنشاء طلب من مدخل وحيد: نص، أو صوت، أو صورة (multipart) → OrderConfirmation JSON.

النص والصوت والصورة تعمل بالكامل. الصورة تحتاج GPU مع transformers/torch/Pillow
مثبَّتة (requirements-gpu.txt) — محلياً بدون GPU ترجع 501 واضحة (انظر
app/features/order_intake/vision.py).
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.config import settings
from app.engine import llm_engine
from app.features.order_intake.prompts import build_order_intake_prompt
from app.features.order_intake.transcribe import transcribe
from app.features.order_intake.vision import image_describer
from app.features.sales.service import resolve_order
from app.order_schema import OrderConfirmation, OrderExtraction, parse_order_extraction
from app.rag import search as search_words

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["order_intake"])

_PHONE_RE = re.compile(r"07\d{9}")


@router.post("/create", response_model=OrderConfirmation)
async def create_order(
    text: Optional[str] = Form(None),
    audio: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
):
    provided = [v for v in (text, audio, image) if v is not None]
    if len(provided) != 1:
        raise HTTPException(400, "زوّد مدخل واحد بس: text أو audio أو image.")

    if text is not None:
        raw_text = text
    elif audio is not None:
        audio_bytes = await audio.read()
        raw_text = transcribe(audio_bytes)
        if raw_text is None:
            raise HTTPException(503, "تحويل الصوت لنص غير متوفر محلياً (يحتاج transformers مثبَّتة).")
        if not raw_text:
            raise HTTPException(422, "ما كدرنا نفهم أي كلام بالملف الصوتي.")
    else:
        try:
            raw_text = await image_describer.describe(await image.read())
        except NotImplementedError as exc:
            raise HTTPException(501, str(exc))

    rag_words = search_words(raw_text, top_k=settings.rag_top_k)
    messages = build_order_intake_prompt(raw_text, rag_words)

    if llm_engine.ready:
        prompt = llm_engine.render_prompt(messages)
        schema = OrderExtraction.model_json_schema()
        raw_json = await llm_engine.generate_full(
            prompt, max_tokens=512, temperature=0.0, guided_json=schema
        )
        extraction = parse_order_extraction(raw_json)
        if not extraction.items:
            # الموديل مدرَّب على ردود مبيعات عراقية قصيرة، وبدون guided
            # decoding (كان ميزة vLLM، غير مدعوم بـ transformers) قد يرد
            # بلهجة عراقية بدل JSON فيفشل التحليل بصمت. نسجّل الناتج الخام
            # للتشخيص ونرجع لاستخراج بدائي: النص كاملاً كاسم منتج (resolve_order
            # يطابقه على الكتالوج بـ BM25) + رقم الهاتف بـ regex.
            logger.warning("استخراج JSON فشل — الناتج الخام من الموديل: %r", raw_json[:500])
            phone_match = _PHONE_RE.search(raw_text)
            extraction = OrderExtraction(
                customer_phone=phone_match.group() if phone_match else None,
                items=[{"product_name": raw_text, "quantity": 1}],
            )
    else:
        extraction = OrderExtraction(items=[{"product_name": raw_text, "quantity": 1}])

    return await resolve_order(extraction)
