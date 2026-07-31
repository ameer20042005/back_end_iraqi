# -*- coding: utf-8 -*-
"""يحوّل OrderExtraction (خام من الموديل) إلى OrderConfirmation موثوق.

**لا كتالوج محلي نطابق عليه أسعار المنتجات**: الباك اند (الذكاء) لا يخزّن
ولا يفهرس أي بيانات منتج. الموديل نفسه يستدعي أداة search_products أثناء
المحادثة (انظر app/tools/products.py) فيشوف السعر الحقيقي لحظياً، ويذكره
بمخطط الاستخراج (plane.md → PlaneOrderExtraction.price → quoted_price) —
هذا هو المصدر الوحيد لسعر/مجموع الطلب هنا. لا نعيد حساب أي رقم، فقط نثق
بما استخرجه الموديل من محادثة رأت الأسعار الحقيقية فعلاً.

هذا فرق جوهري عن التصميم القديم (كان يطابق اسم كل صنف بكتالوج محلي ويحسب
subtotal/total بنفسه بدل الثقة بأرقام الموديل) — الثقة انتقلت من "كتالوج
محلي ثابت" إلى "نتيجة استعلام حي رآها الموديل بنفس المحادثة"، وحارس الأرقام
(app/guards.py) يبقى خط الدفاع ضد أي رقم لم يُذكر فعلياً بالمحادثة.
"""

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import List

from app.order_gateway import order_submitter
from app.order_schema import OrderConfirmation, OrderExtraction, ResolvedOrderItem

logger = logging.getLogger(__name__)

_PHONE_RE = re.compile(r"^07\d{9}$")

# كلمات تأكيد/ردود قصيرة تطلع اسم منتج عند فشل الاستخراج ورجوعه للمسار
# البدائي (يبني الطلب من آخر رسالة عميل، وهي عادةً «نعم»).
_NOT_A_PRODUCT = {
    "نعم", "اي", "اكيد", "أكيد", "زين", "تمام", "اوكي", "اوك", "خلص",
    "موافق", "زبطت", "ثبت", "ثبتها", "ثبته", "شكرا", "شكراً", "هلو", "مرحبا",
}


def _new_order_id() -> str:
    """معرّف طلب بصيغة النظام ORD-####.

    كان `str(uuid.uuid4())` — معرّف تقني بـ36 خانة يظهر للزبون بواجهة الشات
    («طلب مؤكَّد — 316f2f31-8764-4d31-...»). صيغة النظام الفعلية هي ORD-####،
    وهي اللي يعرف يقراها الزبون ويلگاها الدعم لما يستعلم عنها. الجزء العشوائي
    يبقى: ٦ خانات من uuid4 حتى يبقى المعرّف فريداً بلا عدّاد مشترك بين العمليات.

    **أرقام فقط** لا خانات سداسية عشرية: مستخرِج الدعم
    (`app/features/support/router.py: _ORDER_ID_RE`) يقرأ `ORD-` متبوعاً بأرقام،
    فمعرّف مثل «ORD-B1061E» ما يلگاه الدعم لما يستعلم عنه الزبون. وهي أسهل على
    الزبون يقراها بالهاتف. تسع خانات: احتمال التصادم مهمل بحجم الطلبات المتوقَّع.

    ملاحظة للربط الحقيقي: لو صار نظام الطلبات الخارجي هو اللي يولّد المعرّف،
    استبدل هذا بالمعرّف الراجع منه بدل ما تولّد واحداً محلياً."""
    return f"ORD-{uuid.uuid4().int % 1_000_000_000:09d}"


def _submission_blockers(order: OrderConfirmation) -> List[str]:
    """أسباب منع إرسال الطلب لنظام الطلبات الخارجي — فحص المخرَج الأخير.

    ليش موجود: بوابة الاكتمال بـ app/features/sales/router.py تحرس **المدخل**
    (هل ذكر العميل هاتفه وعنوانه؟)، لكن بينها وبين الإرسال تجري خطوة استخراج
    بالموديل ممكن تفشل. عند فشلها يرجع المسار البدائي (_fallback_extraction)
    طلباً مبنياً من آخر رسالة عميل — وآخر رسالة عادةً «نعم» — فيطلع طلب
    اسم منتجه «نعم» بلا اسم ولا هاتف ويُرسل كأنه صحيح. الحارس هنا يقطع
    هذا: طلب ناقص يُرجَع للعميل بالـ API لكن ما يدخل نظام الطلبات."""
    blockers = []
    if not (order.customer_phone and _PHONE_RE.match(order.customer_phone.strip())):
        blockers.append("phone")
    if not (order.customer_name and order.customer_name.strip()):
        blockers.append("name")
    if not (order.customer_city or order.customer_district or order.customer_address):
        blockers.append("location")
    real_items = [
        i for i in order.items
        if i.product_name.strip() and i.product_name.strip() not in _NOT_A_PRODUCT
    ]
    if not real_items:
        blockers.append("items")
    return blockers


def _parse_quoted_price(quoted_price: str) -> float:
    """يحوّل quoted_price النصي (رقم فقط حسب قاعدة plane.md §3) لرقم عشري،
    أو 0.0 إذا ما انحلّل. لا تقريب ولا تخمين — مرجع الرقم الوحيد هو ما كتبه
    الموديل بنفس صيغة plane.md (أرقام لا فواصل ولا عملة)."""
    try:
        return float(quoted_price)
    except (TypeError, ValueError):
        return 0.0


async def resolve_order(extraction: OrderExtraction, api_key: str) -> OrderConfirmation:
    resolved_items = [
        ResolvedOrderItem(
            product_name=item.product_name,
            quantity=item.quantity,
        )
        for item in extraction.items
    ]

    # المجموع مصدره الوحيد quoted_price — ما استخرجه الموديل من محادثة رأت
    # الأسعار الحقيقية فعلاً عبر أداة search_products. بلا quoted_price ماكو
    # مجموع نثق فيه (None لا 0.0 — الصفر يُقرأ "الطلب مجاني").
    total = _parse_quoted_price(extraction.quoted_price) if extraction.quoted_price else None

    note = extraction.confirmation_note or "تم تثبيت طلبك، وياتك بأقرب وقت ان شاء الله."

    confirmation = OrderConfirmation(
        order_id=_new_order_id(),
        created_at=datetime.now(timezone.utc).isoformat(),
        customer_name=extraction.customer_name,
        customer_phone=extraction.customer_phone,
        customer_phone2=extraction.customer_phone2,
        customer_address=extraction.customer_address,
        customer_city=extraction.customer_city,
        customer_district=extraction.customer_district,
        state_code=extraction.state_code,
        items=resolved_items,
        subtotal=total,
        total=total,
        quoted_price=extraction.quoted_price,
        notes=extraction.notes,
        confirmation_message=note,
    )

    # فحص المخرَج الأخير قبل نظام الطلبات — انظر _submission_blockers.
    blockers = _submission_blockers(confirmation)
    if blockers:
        logger.warning(
            "طلب ناقص ما انرسل لنظام الطلبات — الناقص: %s (order_id=%s)",
            blockers, confirmation.order_id,
        )
        return confirmation

    # يرسل الطلب المؤكَّد لنظام إدارة الطلبات الخارجي. لا نفشل تسليم الرد
    # للعميل لو تعذّر الإرسال؛ الطلب يبقى موجوداً بالرد على أي حال ويمكن
    # إعادة محاولة إرساله لاحقاً.
    try:
        await order_submitter.submit(confirmation, api_key)
    except Exception:
        logger.exception("فشل إرسال الطلب لنظام الطلبات (order_id=%s)", confirmation.order_id)

    return confirmation
