# -*- coding: utf-8 -*-
"""بوابة إرسال قرار مكالمة "راجع" لباك اند السستم — إخراج فقط، بلا أي
تخزين محلي. نفس فلسفة app/order_gateway.py وvoice_followup/gateway.py.

تُستدعى **مرة وحدة بس** بنهاية المكالمة، بعد ما يُحسم القرار فعلياً — لا
نرسل شي وسط المكالمة، حتى لا تنسجّل حالة ما وافق عليها الزبون نهائياً.

TODO: مسار باك اند السستم الفعلي غير معروف بعد —
`/orders/{order_id}/return-decision` أدناه أفضل تخمين موثَّق (يوازي
`/orders/{order_id}/feedback` بـvoice_followup). عدّل المسار فقط عند توفر
التفاصيل الحقيقية؛ الواجهة (VoiceReturnSubmitter) والمستدعي (router.py)
لا يتغيّرون.
"""

from abc import ABC, abstractmethod
from typing import Optional

import httpx

from app.config import settings
from app.features.voice_return.schema import VoiceReturnOrderRequest
from app.system_backend import request as backend_request


# الشرح: الواجهة المجرّدة. وجودها مو ترفاً معمارياً: هي اللي تخلي الراوتر
# قابلاً للاختبار بلا شبكة (تبديل المنفّذ بمزيّف بالاختبارات)، ونفس النمط
# متبع بكل بوابات المشروع.
class VoiceReturnSubmitter(ABC):
    @abstractmethod
    async def submit(
        self,
        order: VoiceReturnOrderRequest,
        decision: str,
        action: str,
        new_status: Optional[str],
        reason_key: Optional[str],
        customer_reason_text: Optional[str],
        api_key: str,
    ) -> bool:
        """يرسل قرار المكالمة النهائي. يرجع True لو نجح الإرسال."""


# الشرح: المنفّذ الفعلي عبر HTTP.
#
# نرسل `decision` و`action` و`new_status` الثلاثة مع بعض عمداً، مع إن
# الأخيرين مشتقّان من الأول عبر DECISION_MAP: الاشتقاق صار **عندنا**
# بالخادم (لا نثق باقتراح النموذج — قاعدة الدليل)، وإرسال النتيجة كاملة
# يخلي باك اند السستم ما يحتاج يعرف الخريطة ولا يعيد حسابها، فتبقى نسخة
# وحدة منها بمكان واحد.
#
# `new_status=None` تعني "لا تغيّر حالة الشحنة" (حالة NO_ACTION_NEEDED:
# رقم غلط أو شخص غير مخوّل) — نمررها كما هي بدل ما نحذف الحقل، حتى يكون
# الفرق بين "ما في حالة جديدة" و"نسينا نرسلها" صريحاً بالجسم.
class HttpVoiceReturnSubmitter(VoiceReturnSubmitter):
    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url or settings.system_backend_base_url
        self._timeout = timeout

    async def submit(
        self,
        order: VoiceReturnOrderRequest,
        decision: str,
        action: str,
        new_status: Optional[str],
        reason_key: Optional[str],
        customer_reason_text: Optional[str],
        api_key: str,
    ) -> bool:
        payload = {
            "order_id": order.order_id,
            "decision": decision,
            "recommended_action": action,
            "new_status": new_status,
            "return_reason_key": reason_key,
            "customer_reason_text": customer_reason_text,
            "notes": f"تمت المعالجة: مكالمة راجع — {decision}",
        }
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await backend_request(
                client, "POST", f"/orders/{order.order_id}/return-decision",
                json=payload,
                headers={"X-API-Key": api_key},
            )
            resp.raise_for_status()
            return True


# الشرح: النسخة الوحيدة المستخدمة عبر التطبيق. متغيّر وحدة (module-level
# singleton) حتى الاختبارات تگدر تبدّله بمزيّف بسطر واحد.
voice_return_submitter: VoiceReturnSubmitter = HttpVoiceReturnSubmitter()
