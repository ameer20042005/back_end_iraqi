# -*- coding: utf-8 -*-
"""بوابة نظام إدارة الطلبات — استعلام/إرسال حقيقي لحظي، بلا أي تخزين محلي.

واجهتان منفصلتان لكل اتجاه بيانات:
- **إخراج (Outbound)** — `OrderStatusProvider`: نستعلم منه (تتبع حالة طلب
  برقم الطلب أو الهاتف). يستخدمها `app/features/support/`.
- **إدخال (Inbound)** — `OrderSubmitter`: نرسل له طلباً جديداً بعد ما تحسبه
  `app/features/sales/service.py` (أو `order_intake`).

**لا تخزين محلي بأي شكل**: كل استدعاء يذهب فوراً لباك اند السستم عبر HTTP،
مصادقاً بمفتاح API (انظر app/auth.py)، والنتيجة تُعالَج وتُرجَع للمتصل بلا
أي احتفاظ بها هنا. البيانات تصل لحظياً وقت الاستدعاء فقط.

TODO: رابط ومسارات باك اند السستم الفعلية غير معروفة بعد — `SYSTEM_BACKEND_BASE_URL`
(app/config.py) ومسارات `search`/`_headers` أدناه أفضل تخمين موثَّق. عدّلها
فقط عند توفر التفاصيل الحقيقية؛ الواجهة (OrderStatusProvider/OrderSubmitter)
والمستدعين (support/sales) لا يتغيّرون.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

import httpx

from app.config import settings
from app.order_schema import OrderConfirmation
from app.system_backend import request as backend_request

# ---------------------------------------------------------------------------
# إخراج (Outbound) — استعلام حالة طلب
# ---------------------------------------------------------------------------


class OrderStatusProvider(ABC):
    @abstractmethod
    async def get_by_order_id(self, order_id: str, api_key: str) -> Optional[dict]:
        """يرجع حالة طلب واحد بمعرّفه، أو None إذا غير موجود."""

    @abstractmethod
    async def search_by_phone(self, phone: str, api_key: str) -> List[dict]:
        """يرجع كل الطلبات المرتبطة برقم هاتف."""

    @abstractmethod
    async def search_by_status(self, status: str, api_key: str) -> List[dict]:
        """يرجع كل الطلبات بحالة معينة («قيد التوصيل»، «تم التسليم»...).

        هذي عملية داخلية للموظفين (تتبع تشغيلي)، مو استعلام زبون — البوت
        داخلي بحت."""

    @abstractmethod
    async def list_all(self, api_key: str) -> List[dict]:
        """يرجع كل الطلبات (لأسئلة الجرد: «كم طلب عدنا؟»)."""


class HttpOrderStatusProvider(OrderStatusProvider):
    """عميل HTTP رفيع لباك اند السستم — بلا أي تخزين أو فهرسة محلية."""

    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url or settings.system_backend_base_url
        self._timeout = timeout

    def _headers(self, api_key: str) -> dict:
        return {"X-API-Key": api_key}

    async def _get(self, path: str, api_key: str, params: Optional[dict] = None) -> Optional[dict]:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await backend_request(
                client, "GET", path, params=params, headers=self._headers(api_key)
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    async def get_by_order_id(self, order_id: str, api_key: str) -> Optional[dict]:
        return await self._get(f"/orders/{order_id}", api_key)

    async def search_by_phone(self, phone: str, api_key: str) -> List[dict]:
        data = await self._get("/orders/search", api_key, params={"phone": phone})
        return (data or {}).get("orders", [])

    async def search_by_status(self, status: str, api_key: str) -> List[dict]:
        data = await self._get("/orders/search", api_key, params={"status": status})
        return (data or {}).get("orders", [])

    async def list_all(self, api_key: str) -> List[dict]:
        data = await self._get("/orders", api_key)
        return (data or {}).get("orders", [])


order_status_provider: OrderStatusProvider = HttpOrderStatusProvider()


# ---------------------------------------------------------------------------
# إدخال (Inbound) — تثبيت طلب جديد بالنظام الخارجي
# ---------------------------------------------------------------------------


class OrderSubmitter(ABC):
    @abstractmethod
    async def submit(self, order: OrderConfirmation, api_key: str) -> bool:
        """يرسل طلباً مؤكَّداً للنظام الخارجي. يرجع True لو نجح الإرسال."""


class HttpOrderSubmitter(OrderSubmitter):
    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url or settings.system_backend_base_url
        self._timeout = timeout

    async def submit(self, order: OrderConfirmation, api_key: str) -> bool:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await backend_request(
                client, "POST", "/orders",
                json=order.model_dump(),
                headers={"X-API-Key": api_key},
            )
            resp.raise_for_status()
            return True


order_submitter: OrderSubmitter = HttpOrderSubmitter()
