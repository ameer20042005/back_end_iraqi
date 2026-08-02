# -*- coding: utf-8 -*-
"""بوابة إرسال نتيجة المتابعة الصوتية لباك اند السستم — إدخال (Inbound) فقط،
بلا أي تخزين محلي. نفس فلسفة app/order_gateway.py (OrderSubmitter): استعلام
لحظي حقيقي عبر HTTP، مصادَق بمفتاح API، والنتيجة تُعالَج وتُرجَع للمتصل بلا
احتفاظ بها هنا.

الـ query المُرسَل يحمل بيانات الزبون كاملة (اسم/هاتف/موقع) من الطلب الأصلي
اللي فتح الجلسة (POST /ask) + ملخّص السبب المستخرَج من رد الزبون الصوتي —
كما طُلب: التوثيق يجمع مصدرين (JSON الطلب الأصلي + تحليل الرد) بجسم واحد.

TODO: مسار باك اند السستم الفعلي لاستقبال هذا الـ query غير معروف بعد —
`/orders/{order_id}/feedback` أدناه أفضل تخمين موثَّق (يوازي POST /orders
بـ app/order_gateway.py). عدّل المسار فقط عند توفر التفاصيل الحقيقية؛ الواجهة
(VoiceFollowupSubmitter) والمستدعي (router.py) لا يتغيّرون.
"""

from abc import ABC, abstractmethod

import httpx

from app.config import settings
from app.features.voice_followup.schema import VoiceFollowupOrderRequest
from app.system_backend import request as backend_request


class VoiceFollowupSubmitter(ABC):
    @abstractmethod
    async def submit(
        self,
        order: VoiceFollowupOrderRequest,
        reason_summary: str,
        customer_transcript: str,
        api_key: str,
    ) -> bool:
        """يرسل نتيجة المتابعة الصوتية (سبب الزبون) لباك اند السستم. يرجع
        True لو نجح الإرسال."""


class HttpVoiceFollowupSubmitter(VoiceFollowupSubmitter):
    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url or settings.system_backend_base_url
        self._timeout = timeout

    async def submit(
        self,
        order: VoiceFollowupOrderRequest,
        reason_summary: str,
        customer_transcript: str,
        api_key: str,
    ) -> bool:
        # بيانات الزبون كاملة من الطلب الأصلي (المصدر الوحيد الموثوق —
        # لا نستنتج اسماً أو رقماً أو موقعاً من رد الزبون الصوتي نفسه) +
        # ملخّص السبب المستخرَج من رده. نفس مبدأ resolve_order بـ
        # app/features/sales/service.py: لا نعيد حساب أو نخترع، فقط نمرر
        # ما وصل فعلاً من مصادر موثوقة.
        payload = {
            "order_id": order.order_id,
            "status": order.status,
            "customer_name": order.customer_name,
            "customer_phone": order.customer_phone,
            "customer_city": order.customer_city,
            "customer_district": order.customer_district,
            "customer_address": order.customer_address,
            "items": [i.model_dump() for i in order.items],
            "reason_summary": reason_summary,
            "customer_transcript": customer_transcript,
        }
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await backend_request(
                client, "POST", f"/orders/{order.order_id}/feedback",
                json=payload,
                headers={"X-API-Key": api_key},
            )
            resp.raise_for_status()
            return True


voice_followup_submitter: VoiceFollowupSubmitter = HttpVoiceFollowupSubmitter()
