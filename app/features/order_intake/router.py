# -*- coding: utf-8 -*-
"""إنشاء طلب من مدخل وحيد: نص، أو صوت، أو صورة (multipart) → OrderConfirmation JSON.

النص والصوت يعملان بالكامل. الصورة ترجع 501 واضحة حتى تُفعَّل app/features/order_intake/vision.py.
"""

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

router = APIRouter(prefix="/orders", tags=["order_intake"])


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
    else:
        extraction = OrderExtraction(items=[{"product_name": raw_text, "quantity": 1}])

    return resolve_order(extraction)
