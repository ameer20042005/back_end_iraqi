# -*- coding: utf-8 -*-
"""إنشاء طلب من مدخل وحيد: نص، أو صوت، أو صورة (multipart) → OrderConfirmation JSON.

النص والصوت يمران بمسار واحد (نص → استخراج). الصورة تمر مباشرة للموديل البصري
مع نفس برومبت plane.md باستدعاء واحد (انظر app/features/order_intake/vision.py)
— بدون خطوة وصف وسيطة كانت تُضيّع الهواتف والأسعار وتضاعف زمن الاستجابة.

الصورة تحتاج GPU مع Pillow مثبَّتة (requirements-gpu.txt) — محلياً بدون GPU
ترجع 501 واضحة.
"""

import logging
import re
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.auth import require_orders_api_key
from app.config import settings
from app.engine import llm_engine
from app.features.order_intake.prompts import build_order_intake_prompt
from app.features.order_intake.transcribe import transcribe
from app.features.order_intake.vision import order_image_reader
from app.features.sales.service import resolve_order
from app.order_extraction import correct_location, state_code_for
from app.order_schema import (
    OrderConfirmation,
    OrderExtraction,
    PlaneOrderExtraction,
    parse_plane_extraction,
)
from app.rag import search_locations
from app.rag import search as search_words

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orders", tags=["order_intake"])

_PHONE_RE = re.compile(r"07\d{9}")


@router.post("/create", response_model=OrderConfirmation)
async def create_order(
    text: Optional[str] = Form(None),
    audio: Optional[UploadFile] = File(None),
    image: Optional[UploadFile] = File(None),
    api_key: str = Depends(require_orders_api_key),
):
    provided = [v for v in (text, audio, image) if v is not None]
    if len(provided) != 1:
        raise HTTPException(400, "زوّد مدخل واحد بس: text أو audio أو image.")

    image_bytes = None
    if text is not None:
        raw_text = text
    elif audio is not None:
        audio_bytes = await audio.read()
        # transcribe() تزامنية وثقيلة (استدلال Whisper) — تشغيلها مباشرة داخل
        # async يجمّد الـ event loop فيتوقف **كل** الطلبات المتزامنة طول النسخ.
        # نرميها لخيط منفصل حتى يبقى الخادم يستجيب.
        raw_text = await run_in_threadpool(transcribe, audio_bytes)
        if raw_text is None:
            raise HTTPException(503, "تحويل الصوت لنص غير متوفر محلياً (يحتاج transformers مثبَّتة).")
        if not raw_text:
            raise HTTPException(422, "ما كدرنا نفهم أي كلام بالملف الصوتي.")
    else:
        image_bytes = await image.read()
        # ماكو نص نستعلم بيه RAG قبل قراءة الصورة — الموديل البصري يقرأ الصورة
        # والبرومت معاً بنفس الاستدعاء، فكتل RAG تُحقن فارغة (تصحيح الموقع
        # الحتمي بـ _correct_location يشتغل بعدها على المخرَج على أي حال).
        raw_text = ""

    rag_words = search_words(raw_text, top_k=settings.rag_top_k) if raw_text else []
    # مرجع المواقع (states.xlsx + districts.xlsx → app/rag/locations.json):
    # نطابق نص الزبون مع أسماء المناطق قبل التوليد ونحقن النتائج بالبرومت
    # حتى يختار الموديل المحافظة الصحيحة بدل التخمين.
    rag_locations = search_locations(raw_text) if raw_text else []
    messages = build_order_intake_prompt(raw_text, rag_words, rag_locations)

    if llm_engine.ready or image_bytes is not None:
        schema = PlaneOrderExtraction.model_json_schema()
        if image_bytes is not None:
            try:
                raw_json = await order_image_reader.extract(image_bytes, messages, schema)
            except NotImplementedError as exc:
                raise HTTPException(501, str(exc))
        else:
            # 384: مخطط plane.md أطول من مخطط المبيعات القديم (city/district/
            # address/phone2/price + orders)، و256 كانت تقصّ الـ JSON بالرسائل
            # المليانة، مع بقاء السقف واطئاً لأن كل توكن زائد وقت فعلي (فك
            # تشفير eager تسلسلي).
            raw_json = await llm_engine.generate_full(
                llm_engine.render_prompt(messages),
                max_tokens=384, temperature=0.0, guided_json=schema,
            )
        plane = parse_plane_extraction(raw_json)
        if plane is None or not plane.orders:
            # guided decoding (vLLM structured outputs) يقيّد الناتج بالمخطط
            # فعلياً، لكن نبقي مسار الفشل دفاعياً (خادم قديم/إعداد ناقص قد
            # يتجاهل response_format فيرد الموديل بلهجة عراقية بدل JSON).
            # نسجّل الناتج الخام للتشخيص ونرجع لاستخراج بدائي: النص كاملاً
            # كاسم منتج (resolve_order يطابقه بالكتالوج بـ BM25) + الهاتف بـ regex.
            logger.warning("استخراج JSON فشل — الناتج الخام من الموديل: %r", raw_json[:500])
        if plane is None:
            # مع الصورة ماكو raw_text نرجع له (الموديل البصري هو الوحيد اللي
            # شاف المحتوى) — فالفشل هنا يعني فشل قراءة كاملاً، نرجع 422 بدل
            # طلب فارغ باسم منتج فاضي.
            if image_bytes is not None:
                raise HTTPException(422, "ما كدرنا نقرأ الطلب من الصورة — جرّب صورة أوضح.")
            phone_match = _PHONE_RE.search(raw_text)
            extraction = OrderExtraction(
                customer_phone=phone_match.group() if phone_match else None,
                items=[{"product_name": raw_text, "quantity": 1}],
            )
        else:
            extraction = correct_location(plane).to_order_extraction()
            extraction.state_code = state_code_for(extraction.customer_city)
    else:
        extraction = OrderExtraction(items=[{"product_name": raw_text, "quantity": 1}])

    return await resolve_order(extraction, api_key)


# تصحيح الموقع وكود المحافظة انتقلا لـ app/order_extraction.py حتى يشاركهما
# مسار /sales/chat (نفس مخطط plane.md لكل مصادر الطلب).
