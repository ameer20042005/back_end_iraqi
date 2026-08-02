# -*- coding: utf-8 -*-
"""وكيل المبيعات: POST /sales/chat و /sales/chat/stream.

الرد مقيَّد بمخطط app.tool_loop.TOOL_LOOP_SCHEMA: الموديل يطلب أداة
search_products صراحةً متى احتاج بيانات منتج، ويعلن اكتمال الطلب بحقل
order_ready (بدل حقن كتالوج RAG تلقائي وعلامة [ORDER_READY] النصية).

عندما يكتمل الطلب (order_ready=true) — نشغّل تلقائياً جولة توليد ثانية
ببرومت plane.md نفسه لاستخراج JSON، ونحسب الأسعار/المجموع من الكتالوج
الحقيقي بدل الثقة بأرقام الموديل.

صيغة الطلب موحّدة مع /orders/create حرفياً (مخطط plane.md عبر
app/order_extraction.py)، فما تختلف حسب مصدر الطلب.
"""

import json
import logging
import re
import uuid
from functools import partial
from typing import List, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import sessions
from app.auth import require_sales_api_key
from app.features.order_intake.prompts import build_order_intake_prompt
from app.features.sales.prompts import build_sales_prompt
from app.features.sales.service import resolve_order
from app.order_extraction import correct_location, state_code_for
from app.order_schema import (
    OrderConfirmation,
    OrderExtraction,
    OrderItemExtraction,
    PlaneOrderExtraction,
    parse_plane_extraction,
)
from app.engine import llm_engine
from app.rag import search_locations
from app.rag import search as search_words
from app.tool_loop import build_schema, run_with_tools
from app.tools.products import search_products_tool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sales", tags=["sales"])

_SESSION_PREFIX = "sales:"

_PHONE_IN_TEXT_RE = re.compile(r"07\d{9}")

# اسم شخص معقول: حروف عربية أو لاتينية فقط، بلا أرقام ولا رموز.
# اللاتينية مقبولة لأن قسم من الزبائن يكتب اسمه «Ameer Wisam».
_NAME_RE = re.compile(r"^[؀-ۿa-zA-Z\s]{3,60}$")
# كلمات تنفي أن الرسالة القصيرة اسم — تأكيد أو طلب لا تعريف بالنفس.
_NOT_A_NAME = {
    "ثبت", "ثبتها", "ثبته", "اي اكيد", "اي أكيد", "اكيد", "أكيد", "نعم",
    "موافق", "زبطت", "تمام", "اي زين", "اوكي", "اوك", "خلص",
    "احجز", "احجزلي", "اريد", "ابي", "شكرا", "هلو", "مرحبا", "السلام",
    "عفوا", "عفواً", "منو", "شنو", "شلون", "بكم", "سعر", "كافي", "زين",
}
# سؤال الوكيل عن الاسم — إذا سبق رسالة العميل، فرسالته جواب أي اسم حتى لو
# كلمة وحدة («امير»)، وهذا اللي كان يخلي البوابة تعيد السؤال بلا داعي.
_NAME_QUESTION_HINTS = ("اسمك", "أسمك", "اسم الكريم", "شنو اسم", "باسم منو")
# كلمات تدل أن الرسالة عنوان أو نية شراء لا تعريفاً بالنفس.
_LOCATION_HINTS = {
    "من", "اني", "عنواني", "عنوان", "سكنة", "ساكن", "محلة", "حي", "منطقة",
    "شارع", "قرب", "مكاني", "التوصيل", "وصلها", "ابعتها",
}

# ترتيب سؤال الحقول الناقصة — نفس تسلسل بائع حقيقي.
_FIELD_QUESTIONS = {
    "product": "تدلل حبيبي، أي منتج تحب أحجزلك ياه بالضبط؟",
    "name": "أمرك حبيبي، أشگد أسمك الكريم حتى أسجّل الطلب باسمك؟",
    "phone": "الله يخليك، أنطيني رقم هاتفك (07...) حتى نتواصل وياك للتوصيل.",
    "location": "زين حبيبي، أشگد محافظتك والمنطقة/الحي حتى نظبّط التوصيل؟",
}
_FIELD_ORDER = ("product", "name", "phone", "location")

# مخطط رد المبيعات: نفس أساس tool_loop + order_ready (بدل علامة [ORDER_READY]
# النصية) — الموديل يعلن اكتمال الطلب صراحةً بحقل مقيَّد guided decoding.
_SALES_SCHEMA = build_schema(
    extra_properties={"order_ready": {"type": "boolean"}},
    extra_required=["order_ready"],
)


def _user_messages(history: List[dict], user_message: str) -> List[str]:
    """رسائل العميل وحده — كلام الوكيل ممنوع يكون مصدر بيانات الطلب."""
    return [m["content"] for m in history if m.get("role") == "user"] + [user_message]


def _looks_like_name(text: str, min_words: int) -> bool:
    text = text.strip()
    if not _NAME_RE.match(text):
        return False
    words = text.split()
    if len(words) < min_words or len(words) > 5:
        return False
    if any(w in text for w in _NOT_A_NAME):
        return False
    # جملة عنوان («اني من بغداد الحارثية») تعدّي فحص الحروف والطول لكنها مو
    # اسم — بلا هذا الفحص كانت تنحفظ كاسم عميل بالطلب.
    if any(w in words for w in _LOCATION_HINTS):
        return False
    return not search_locations(text, top_k=1)


def _customer_name(history: List[dict], user_message: str = "") -> Optional[str]:
    """اسم العميل كما عرّف بنفسه، أو None.

    إشارتان: (١) رسالة باسم مركّب (كلمتان فأكثر) بأي وقت، (٢) رسالة كلمة
    وحدة جاءت مباشرة بعد سؤال الوكيل عن الاسم — بلا الثانية كان العميل اللي
    يجاوب «امير» يُسأل عن اسمه مرة ثانية بلا داعي.

    ترجع الاسم نفسه لا bool حتى تستعمله بوابة الاكتمال والاستخراج البدائي
    سوية: كانتا منفصلتين فمرّ طلب أثبتت البوابة أن العميل عرّف بنفسه فيه
    لكن الاسم ما وصل الطلب (customer_name=None) فحجبه حارس الإرسال."""
    texts = _user_messages(history, user_message) if user_message else [
        m["content"] for m in history if m.get("role") == "user"
    ]
    for t in texts:
        if _looks_like_name(t, min_words=2):
            return t.strip()
    # كلمة وحدة تُقبل فقط كجواب مباشر على سؤال الوكيل عن الاسم.
    turns = history + ([{"role": "user", "content": user_message}] if user_message else [])
    for i, msg in enumerate(turns):
        if msg.get("role") != "user" or i == 0:
            continue
        prev = turns[i - 1]
        if prev.get("role") != "assistant":
            continue
        if any(h in prev.get("content", "") for h in _NAME_QUESTION_HINTS):
            candidate = msg.get("content", "")
            if _looks_like_name(candidate, min_words=1):
                return candidate.strip()
    return None


def _missing_order_fields(
    history: List[dict], user_message: str, session_key: str
) -> List[str]:
    """الحقول الإلزامية الناقصة من كلام العميل نفسه، بترتيب السؤال عنها.

    ليش موجود: قاعدة «لا تثبّت طلب قبل الاسم والهاتف والعنوان» كانت مكتوبة
    بالـ system prompt فقط (app/features/sales/prompts.py) — أي رجاءً للموديل
    لا قيداً عليه. الفحص هنا يفرض التسلسل فعلياً: ما دام حقل ناقص، ما اكو
    تثبيت مهما رجّع الموديل order_ready=true.

    المصدر رسائل العميل حصراً — لو فحصنا المحادثة كاملة لصارت هلوسة الوكيل
    نفسها دليل اكتمال."""
    texts = _user_messages(history, user_message)
    blob = " ".join(texts)
    missing = []

    if not sessions.known_products(session_key):
        missing.append("product")

    if not _customer_name(history, user_message):
        missing.append("name")

    if not _PHONE_IN_TEXT_RE.search(blob):
        missing.append("phone")

    known = sessions.known_location(session_key)
    if not (known.get("city") or known.get("district")):
        missing.append("location")

    return [f for f in _FIELD_ORDER if f in missing]


def _remember_user_location(session_key: str, user_message: str) -> None:
    """يمسح رسالة العميل عن أسماء أماكن ويخزّنها بالجلسة.

    الحي يُذكر عادةً مرة وحدة بأول المحادثة ثم يطلع من نافذة _MAX_TURNS قبل
    التثبيت — انظر sessions.remember_location. المصدر رسالة العميل وحدها
    لنفس سبب _missing_order_fields."""
    city = district = ""
    # نمر على النتائج كلها لا أول وحدة: «بغداد الحارثية» ترجع سطرين —
    # المحافظة أولاً ثم المنطقة — والخروج عند الأول كان يضيّع الحي نفسه.
    for hit in search_locations(user_message, top_k=5):
        if not hit.get("exact"):
            continue
        if hit.get("district"):
            # اسم منطقة متكرر بأكثر من محافظة ما يثبّت المحافظة، بس اسم
            # المنطقة نفسه صالح للحفظ.
            if not district:
                district = hit["district"]
            if not city and len(hit.get("candidates", [])) == 1:
                city = hit["state_name"]
        elif not city:
            city = hit["state_name"]
    sessions.remember_location(session_key, city=city, district=district)


class SalesChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


class SalesChatResponse(BaseModel):
    session_id: str
    answer: str
    order: Optional[OrderConfirmation] = None
    engine: str


def _fallback_sales_answer(message: str) -> str:
    """يُستخدم فقط إذا لم يكن الموديل متوفراً. بلا موديل ماكو من يستدعي
    أداة search_products (استعلام حي على باك اند السستم)، فما عدنا مصدر
    بيانات نجاوب منه — نطلب من العميل ينتظر بدل اختلاق رد."""
    return "[وضع محلي بدون GPU] الخدمة غير متوفرة مؤقتاً، جرّب بعد شوي."


def _fallback_extraction(history: List[dict], session_key: str) -> OrderExtraction:
    """استخراج بدائي حتمي عند فشل استخراج الموديل.

    يبني من كل ما نعرفه بالجلسة يقيناً بدل رسالة وحدة: المنتج من منتجات
    الجلسة، والهاتف بـ regex، والموقع من ذاكرة الجلسة."""
    user_texts = [m["content"] for m in history if m.get("role") == "user"]
    blob = " ".join(user_texts)
    phone = _PHONE_IN_TEXT_RE.search(blob)
    known = sessions.known_location(session_key)
    products = sessions.known_products(session_key)
    return OrderExtraction(
        customer_name=_customer_name(history),
        customer_phone=phone.group() if phone else None,
        customer_city=known.get("city") or None,
        customer_district=known.get("district") or None,
        customer_address=" - ".join(p for p in (known.get("city"), known.get("district")) if p) or None,
        state_code=state_code_for(known.get("city")),
        # آخر منتج ظهر بالجلسة هو الأقرب لما يجري تثبيته.
        items=[OrderItemExtraction(product_name=products[-1]["name"], quantity=1)] if products else [],
    )


async def _maybe_build_order(session_key: str, api_key: str) -> Optional[OrderConfirmation]:
    """يبني الطلب النهائي من المحادثة بمخطط plane.md — نفس مخطط
    /orders/create حرفياً، حتى يطلع الطلب بصيغة وحدة مهما كان مصدره
    (محادثة مبيعات، أو رسالة/صوت/صورة خام).

    rag_words هنا مرجع لهجة عراقية للاستخراج (شرح كلمات دارجة) — منفصل عن
    استرجاع المنتجات المحذوف من مسار الرد، ويبقى لأنه يحسّن دقة استخراج
    الحقول من كلام العميل الخام."""
    history = sessions.get(session_key)
    if llm_engine.ready:
        # كلام العميل وحده، بلا أسطر «الوكيل:». ليش: مستخرج plane.md يعطي
        # أولوية عالية لعبارات مثل «العنوان» (انظر plane.md §4)، فلمّا هلوس
        # الوكيل بملخّصه «على عنوان بغداد، الحارثية» التقطها المستخرج
        # واعتمدها عنواناً حقيقياً. هلوسة الوكيل لا تصير بيانات طلب.
        conversation = "\n".join(
            m["content"] for m in history if m.get("role") == "user"
        )
        rag_words = search_words(conversation)
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
            extraction = _fallback_extraction(history, session_key)
        else:
            # الحي ذُكر مرة وحدة قبل عشر رسائل وطلع من نافذة الجلسة، فيخرج
            # الاستخراج بمنطقة فارغة. نسدّها من ذاكرة الجلسة قبل التصحيح
            # الحتمي حتى يستفيد منها correct_location بتحديد المحافظة.
            known = sessions.known_location(session_key)
            if not plane.district and known.get("district"):
                plane.district = known["district"]
            if not plane.city and known.get("city"):
                plane.city = known["city"]
            extraction = correct_location(plane).to_order_extraction()
            extraction.state_code = state_code_for(extraction.customer_city)
            # الموديل أحياناً يترك الاسم فارغاً رغم أن العميل عرّف بنفسه
            # صراحةً — فيمر الطلب من بوابة الاكتمال ثم يحجبه حارس الإرسال
            # لنقص الاسم. نسدّه من نفس المصدر اللي اعتمدته البوابة.
            if not extraction.customer_name:
                extraction.customer_name = _customer_name(history)
    else:
        extraction = _fallback_extraction(history, session_key)
    return await resolve_order(extraction, api_key)


async def _run_sales_turn(
    key: str, req: SalesChatRequest, history: List[dict], api_key: str
) -> tuple:
    """يولّد رد المبيعات عبر حلقة الأدوات (search_products) ويرجع
    (answer, order_ready, engine_name)."""
    messages = build_sales_prompt(history, req.message)

    if not llm_engine.ready:
        return _fallback_sales_answer(req.message), False, "fallback"

    tools = {"search_products": partial(search_products_tool, api_key=api_key, session_id=key)}
    data = await run_with_tools(
        messages,
        tools=tools,
        max_tokens=req.max_tokens,
        temperature=req.temperature,
        schema=_SALES_SCHEMA,
    )
    return data["final_answer"], bool(data.get("order_ready")), "vllm"


async def _complete_sales_turn(
    key: str, session_id: str, req: SalesChatRequest, history: List[dict], api_key: str
) -> tuple:
    """يشغّل دور المبيعات كاملاً (توليد + بوابة الاكتمال + حفظ الجلسة + بناء
    الطلب) — مشترك بين /chat و /chat/stream حتى لا يتكرر منطق البوابة."""
    answer, order_ready, engine_name = await _run_sales_turn(key, req, history, api_key)

    _remember_user_location(key, req.message)

    # بوابة الاكتمال: تسبق كل شي وتغلب قرار الموديل نفسه — الموديل يثبّت
    # طلباً ناقصاً ويخترع الفراغات. انظر _missing_order_fields.
    if order_ready:
        missing = _missing_order_fields(history, req.message, key)
        if missing:
            logger.warning(
                "تثبيت طلب ناقص أُلغي — حقول ناقصة: %s (session=%s) — الرد الأصلي: %r",
                missing, session_id, answer[:300],
            )
            order_ready = False
            answer = _FIELD_QUESTIONS[missing[0]]

    sessions.append(key, "user", req.message)
    sessions.append(key, "assistant", answer)

    order = await _maybe_build_order(key, api_key) if order_ready else None
    return answer, order, engine_name


@router.post("/chat", response_model=SalesChatResponse)
async def sales_chat(req: SalesChatRequest, api_key: str = Depends(require_sales_api_key)):
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)

    answer, order, engine_name = await _complete_sales_turn(
        key, session_id, req, history, api_key
    )

    return SalesChatResponse(
        session_id=session_id,
        answer=answer,
        order=order,
        engine=engine_name,
    )


@router.post("/chat/stream")
async def sales_chat_stream(req: SalesChatRequest, api_key: str = Depends(require_sales_api_key)):
    """نفس منطق /chat، مبثوث كقطعة واحدة (مو توكن-بتوكن): الرد مقيَّد
    بمخطط JSON صارم (guided_json) فلازم يكتمل كاملاً قبل ما يصير صالحاً
    للتحليل أصلاً — البث الجزئي لبنية JSON غير مفيد للعميل. الردود قصيرة
    أصلاً فالتأخير مقبول (نفس تبرير النسخة السابقة)."""
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)

    async def event_source():
        answer, order, _ = await _complete_sales_turn(
            key, session_id, req, history, api_key
        )
        yield f"data: {json.dumps({'delta': answer}, ensure_ascii=False)}\n\n"
        yield "data: " + json.dumps(
            {
                "done": True,
                "session_id": session_id,
                "order": order.model_dump() if order else None,
            },
            ensure_ascii=False,
        ) + "\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")
