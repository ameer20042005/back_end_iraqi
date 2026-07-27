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
from app.guards import (
    check_product_names,
    contradicts_availability,
    redact_bad_numbers,
)
from app.order_extraction import correct_location, state_code_for
from app.order_schema import (
    OrderConfirmation,
    OrderExtraction,
    OrderItemExtraction,
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

# اسم شخص معقول: حروف عربية أو لاتينية فقط، بلا أرقام ولا رموز.
# اللاتينية مقبولة لأن قسم من الزبائن يكتب اسمه «Ameer Wisam».
_NAME_RE = re.compile(r"^[؀-ۿa-zA-Z\s]{3,60}$")
# كلمات تنفي أن الرسالة القصيرة اسم — تأكيد أو طلب لا تعريف بالنفس.
_NOT_A_NAME = set(_CONFIRM_KEYWORDS) | {
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
    لا قيداً عليه. موديل 4B تجاهلها بالإنتاج وثبّت طلباً بعد الاسم وحده،
    واخترع عنواناً («بغداد، الحارثية») ما ذكره العميل أبداً. الفحص هنا يفرض
    التسلسل فعلياً: ما دام حقل ناقص، ما اكو تثبيت مهما كتب الموديل.

    المصدر رسائل العميل حصراً — لو فحصنا المحادثة كاملة لصارت هلوسة الوكيل
    نفسها دليل اكتمال، وهذا بالضبط اللي انكسر."""
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

def _full_catalog_text() -> str:
    """نص الكتالوج **كله** — مرجع درع أسماء المنتجات.

    ليش الكتالوج كامل لا المنتجات المسترجَعة: الاسترجاع يبني على رسالة العميل
    الأخيرة، فيرجع لابتوبات لسؤال عن لابتوب. لو قسنا عليه وحده، صار ذكر
    «ماوس لوجيتك» — وهو منتج حقيقي عدنا — «اختراعاً» لأنه مو بنتائج هذا الدور.
    الماركة تُقاس على ما نملكه فعلاً، مو على ما استُرجع صدفةً."""
    return " ".join(
        f"{p['name']} {p.get('description', '')} {' '.join(p.get('tags', []))}"
        for p in product_repository.all_products()
    )


def _catalog_offer_reply(rag_products: List[dict]) -> str:
    """رد بديل حتمي مبني من الكتالوج الحقيقي وحده — يُستخدم لما يخترع الموديل
    منتجاً مو عدنا (انظر check_product_names).

    ما نرجع رد تهرب هنا: الزبون سأل سؤال توفّر ويستاهل جواباً. نعرض اللي
    نملكه فعلاً بأسمائه وأسعاره الحرفية من الكتالوج، وإذا ماكو شي مطابق
    ننفي بصراحة."""
    if not rag_products:
        return (
            "عذراً حبيبي، ما لگيت شي مطابق لطلبك بالمتوفر عدنا هسه. "
            "گلي شنو تحتاج بالضبط وأشوفلك."
        )
    lines = "، ".join(
        f"{p['name']} بـ{p['price']:,} {p.get('currency', 'IQD')}"
        for p in rag_products[:3]
    )
    return f"هلا بيك، اللي متوفر عدنا هسه: {lines}. تحب أفصّلك على أي واحد؟"


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


def _fallback_order_ready(message: str) -> bool:
    return any(kw in message for kw in _PURCHASE_KEYWORDS)


def _fallback_extraction(history: List[dict], session_key: str) -> OrderExtraction:
    """استخراج بدائي حتمي عند فشل استخراج الموديل.

    كان يبني الطلب من آخر رسالة عميل وحدها، وآخر رسالة بأي تثبيت هي كلمة
    التأكيد («نعم») — فيطلع طلب اسم منتجه «نعم» بلا اسم ولا هاتف. نبني هنا
    من كل ما نعرفه بالجلسة يقيناً بدل رسالة وحدة: المنتج من منتجات الجلسة،
    والهاتف بـ regex، والموقع من ذاكرة الجلسة."""
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


async def _maybe_build_order(session_key: str, rag_words: List[dict]) -> Optional[OrderConfirmation]:
    """يبني الطلب النهائي من المحادثة بمخطط plane.md — نفس مخطط
    /orders/create حرفياً، حتى يطلع الطلب بصيغة وحدة مهما كان مصدره
    (محادثة مبيعات، أو رسالة/صوت/صورة خام). كان هذا المسار يستعمل مخططاً
    مختلفاً (customer_name/items) فيرجع للعميل شكل JSON ثاني."""
    history = sessions.get(session_key)
    if llm_engine.ready:
        # كلام العميل وحده، بلا أسطر «الوكيل:». ليش: مستخرج plane.md يعطي
        # أولوية عالية لعبارات مثل «العنوان» (انظر plane.md §4)، فلمّا هلوس
        # الوكيل بملخّصه «على عنوان بغداد، الحارثية» التقطها المستخرج
        # واعتمدها عنواناً حقيقياً. هلوسة الوكيل لا تصير بيانات طلب.
        conversation = "\n".join(
            m["content"] for m in history if m.get("role") == "user"
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
        # درع الأسماء أولاً: منتج مخترع يبطل الرد كله، فما ينفع ننقّح أرقامه
        # ونسلّمه — الرقم المنقَّح بجملة تعرض منتجاً ما نملكه يبقى كذبة.
        invented = check_product_names(answer, _full_catalog_text())
        contradiction = contradicts_availability(answer, _full_catalog_text())
        if invented or contradiction:
            logger.warning(
                "منتج مخترَع برد المبيعات — ماركات: %s، تناقض توفّر: %s "
                "(session=%s) — الرد الأصلي: %r",
                invented, contradiction, session_id, answer[:300],
            )
            answer = _catalog_offer_reply(known_products)
        answer, redacted = redact_bad_numbers(answer, reference_text)
        if redacted:
            logger.warning(
                "أرقام مختلَقة برد المبيعات نُقّحت: %s (session=%s) — الرد بعد التنقيح: %r",
                redacted, session_id, answer[:300],
            )

    # شبكة أمان تحت العلامة: الموديل ينسى [ORDER_READY] ويختم بكلام طبيعي
    # («خوش، تم الطلب») فتضيع الطلبات المكتملة — انظر _order_complete_by_content.
    if not order_ready and engine_name == "vllm" and _order_complete_by_content(
        history, req.message, answer
    ):
        logger.info(
            "الطلب اكتمل بالمحتوى بدون علامة %s (session=%s)", ORDER_READY_MARKER, session_id
        )
        order_ready = True

    _remember_user_location(key, req.message)

    # بوابة الاكتمال: تسبق كل شي وتغلب العلامة نفسها — الموديل يثبّت طلباً
    # ناقصاً ويخترع الفراغات. انظر _missing_order_fields.
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
            # نفس ترتيب مسار /chat — انظر التعليق هناك.
            invented = check_product_names(answer, _full_catalog_text())
            contradiction = contradicts_availability(answer, _full_catalog_text())
            if invented or contradiction:
                logger.warning(
                    "منتج مخترَع برد المبيعات (stream) — ماركات: %s، تناقض توفّر: %s "
                    "(session=%s) — الرد الأصلي: %r",
                    invented, contradiction, session_id, answer[:300],
                )
                answer = _catalog_offer_reply(known_products)
            answer, redacted = redact_bad_numbers(answer, reference_text)
            if redacted:
                logger.warning(
                    "أرقام مختلَقة برد المبيعات (stream) نُقّحت: %s (session=%s) — الرد بعد التنقيح: %r",
                    redacted, session_id, answer[:300],
                )
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

        _remember_user_location(key, req.message)

        # نفس بوابة الاكتمال بمسار البث — انظر _missing_order_fields.
        if order_ready:
            missing = _missing_order_fields(history, req.message, key)
            if missing:
                logger.warning(
                    "تثبيت طلب ناقص أُلغي (stream) — حقول ناقصة: %s (session=%s) — الرد الأصلي: %r",
                    missing, session_id, answer[:300],
                )
                order_ready = False
                answer = _FIELD_QUESTIONS[missing[0]]

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
