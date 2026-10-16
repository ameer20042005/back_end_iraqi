# -*- coding: utf-8 -*-
"""صيغ المكالمات الصوتية: بيانات الطلب كما يرسلها باك اند البيانات (انظر
docs/reference/jenni-stores-schema.md — الحقول تطابق sells/sell_items منطقياً)،
وطلب بدء مكالمة ونتيجته.

بيانات الطلب تمر للـ agent كـ metadata للغرفة، ولا نرسل لباك اند البيانات إلا
ما وصل فعلياً من مصدر موثوق (هذا الطلب) أو ما قاله الزبون — لا نخترع شي."""

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class VoiceFollowupItem(BaseModel):
    product_name: str
    quantity: int = 1


class VoiceFollowupOrderRequest(BaseModel):
    """تفاصيل الطلب. `status` محرّك نوع السؤال بمكالمة المتابعة، بلا قائمة
    ثابتة — قيمتها من نظام البيانات الحقيقي (sell_status أو مرادفاته العربية)."""

    order_id: str
    status: str  # مثلاً: "ملغي"، "مرتجع"، "لم يتم التسليم"، "قيد التوصيل"
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_city: Optional[str] = None
    customer_district: Optional[str] = None
    customer_address: Optional[str] = None
    items: List[VoiceFollowupItem] = Field(default_factory=list)
    reason_hint: Optional[str] = None  # سبب أوّلي إن كان معروفاً بالنظام
    notes: Optional[str] = None
    # موعد التسليم المجدوَل حالياً (ISO "YYYY-MM-DD") — تذكّر بيه صباح الزبون
    # قبل عرض التأجيل.
    scheduled_date: Optional[str] = None


# followup: JENI تسأل عن سبب حالة الطلب (ملغي/مرتجع/لم يتم التسليم).
# postpone: صباح تعرض تأجيل تسليم شحنة لسه ما انسلّمت.
CallType = Literal["followup", "postpone"]


class CallRequest(BaseModel):
    """جسم POST /voice_followup/calls — وهو نفسه وسائط أداة start_call اللي
    يرجعها مساعد الموظف النصي، فتطبيق سطح المكتب يمرّرها كما هي."""

    call_type: CallType
    order: VoiceFollowupOrderRequest


class CallResponse(BaseModel):
    room: str
    livekit_url: str
    # توكن دخول الغرفة لتطبيق سطح المكتب (مراقبة المكالمة أو تسلّمها من الموظف).
    operator_token: str
    dispatch_id: str
    # "sip": الـ agent يتصل بالزبون بنفسه عبر SIP trunk.
    # "bridge": التطبيق يدخل الغرفة بـ operator_token ويربط صوت مكالمة الواتساب.
    dial_mode: Literal["sip", "bridge"]
