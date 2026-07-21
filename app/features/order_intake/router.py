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
from app.order_schema import (
    OrderConfirmation,
    OrderExtraction,
    PlaneOrderExtraction,
    parse_plane_extraction,
)
from app.rag import canonical_state, search_locations, state_for_district
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
    # مرجع المواقع (states.xlsx + districts.xlsx → app/rag/locations.json):
    # نطابق نص الزبون مع أسماء المناطق قبل التوليد ونحقن النتائج بالبرومت
    # حتى يختار الموديل المحافظة الصحيحة بدل التخمين.
    rag_locations = search_locations(raw_text)
    messages = build_order_intake_prompt(raw_text, rag_words, rag_locations)

    if llm_engine.ready:
        prompt = llm_engine.render_prompt(messages)
        schema = PlaneOrderExtraction.model_json_schema()
        # 384: مخطط plane.md أطول من مخطط المبيعات القديم (city/district/
        # address/phone2/price + orders)، و256 كانت تقصّ الـ JSON بالرسائل
        # المليانة، مع بقاء السقف واطئاً لأن كل توكن زائد وقت فعلي (فك
        # تشفير eager تسلسلي).
        raw_json = await llm_engine.generate_full(
            prompt, max_tokens=384, temperature=0.0, guided_json=schema
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
            phone_match = _PHONE_RE.search(raw_text)
            extraction = OrderExtraction(
                customer_phone=phone_match.group() if phone_match else None,
                items=[{"product_name": raw_text, "quantity": 1}],
            )
        else:
            extraction = _correct_location(plane).to_order_extraction()
            extraction.state_code = _state_code_for(extraction.customer_city)
    else:
        extraction = OrderExtraction(items=[{"product_name": raw_text, "quantity": 1}])

    return await resolve_order(extraction)


def _correct_location(plane: PlaneOrderExtraction) -> PlaneOrderExtraction:
    """تصحيح حتمي للموقع بعد الاستخراج، من قاعدة بيانات شركة التوصيل —
    الهدف أن يخرج city دائماً باسم محافظة رسمي من states.xlsx وdistrict
    باسم منطقة رسمي من districts.xlsx متى ما أمكن:

    1. إذا المنطقة المستخرجة معروفة وتتبع محافظة واحدة فقط → محافظتها هي
       city مهما خمّن الموديل (نفس قاعدة plane.md: كلمة العنوان تُصدَّق)،
       وdistrict يُوحَّد على الاسم الرسمي بالقاعدة.
    2. وإلا نوحّد إملاء city المستخرجة على الاسم الرسمي بقاعدة البيانات
       (بصره → البصرة، حله → بابل الحلة...)؛ وإذا ما طابقت أي محافظة
       نبقيها كما وردت.
    """
    by_district = state_for_district(plane.district)
    if by_district is None and not plane.district and plane.address:
        # المنطقة فارغة لكن ربما العنوان الحر يحتوي اسم منطقة معروفة —
        # نمسحه ونعتمد أول مطابقة حرفية غير غامضة (محافظة واحدة فقط)
        for hit in search_locations(plane.address, top_k=3):
            if hit["district"] and hit["exact"] and len(hit["candidates"]) == 1:
                by_district = {
                    "code": hit["state_code"],
                    "name": hit["state_name"],
                    "district": hit["district"],
                }
                break
    if by_district:
        plane.city = by_district["name"]
        plane.district = by_district["district"]
        return plane
    state = canonical_state(plane.city)
    if state:
        plane.city = state["name"]
    return plane


def _state_code_for(city: Optional[str]) -> Optional[str]:
    state = canonical_state(city or "")
    return state["code"] if state else None
