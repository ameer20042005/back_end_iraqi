# -*- coding: utf-8 -*-
"""صيغ مسار المتابعة الصوتية للطلبات: JSON طلب خام يجي من باك اند السستم
(انظر assets/JENNI_STORES_SCHEMA_FOR_AI_QUERY_BUILDER.md — الحقول هنا تطابق
sells/sell_items منطقياً)، وصيغة الجلسة المحفوظة بين POST /ask وPOST /respond.

الفصل بين VoiceFollowupOrder (خام من باك اند السستم) وVoiceFollowupSession
(ما نحفظه بالجلسة) نفس فلسفة OrderExtraction/OrderConfirmation بـ
app/order_schema.py: لا نعيد للزبون ولا نرسل لباك اند السستم إلا بيانات
وصلت فعلياً من مصدر موثوق (JSON الطلب الأصلي)، لا نخترعها."""

from typing import List, Optional

from pydantic import BaseModel, Field


class VoiceFollowupItem(BaseModel):
    product_name: str
    quantity: int = 1


class VoiceFollowupOrderRequest(BaseModel):
    """جسم POST /voice_followup/ask — تفاصيل الطلب كما يرسلها باك اند
    السستم. `status` هي المحرّك الرئيسي لنوع السؤال (انظر prompts.py) —
    قيمتها تُشتق من نظام السستم الحقيقي (catalog.sells.sell_status أو
    مرادفاته العربية)، لا قائمة ثابتة هنا (نفس مبدأ app/guards.py: لا نحمل
    معرفة عن حالات بعينها)."""

    order_id: str
    status: str  # مثلاً: "ملغي"، "مرتجع"، "لم يتم التسليم"...
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_city: Optional[str] = None
    customer_district: Optional[str] = None
    customer_address: Optional[str] = None
    items: List[VoiceFollowupItem] = Field(default_factory=list)
    reason_hint: Optional[str] = None  # سبب أوّلي إن كان معروفاً بالسستم (مثل "رفض الزبون الاستلام")
    notes: Optional[str] = None
