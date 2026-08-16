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

**التحقق من الشكل (إخراج فقط)**: كل استجابة تمر عبر `SystemOrder`
(`app/system_backend_schema.py`) — العقد الموثَّق رسمياً بـ
API.md § "عقد باك اند السستم". طلب لا يطابق العقد يُستبعَد بصمت من القوائم
(`search_by_phone`/`search_by_status`/`list_all`) أو يرجع None بـ
`get_by_order_id`، بدل ما يمرر شكلاً غير متوقَّع للموديل أو للرد الحتمي.

TODO: رابط ومسارات باك اند السستم الفعلية غير معروفة بعد — `SYSTEM_BACKEND_BASE_URL`
(app/config.py) ومسارات `search`/`_headers` أدناه أفضل تخمين موثَّق. عدّلها
فقط عند توفر التفاصيل الحقيقية؛ الواجهة (OrderStatusProvider/OrderSubmitter)
والمستدعين (support/sales) لا يتغيّرون.
"""

import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

import httpx
from pydantic import ValidationError

from app.config import settings
from app.order_schema import OrderConfirmation
from app.system_backend import request as backend_request
from app.system_backend_schema import SystemOrder

logger = logging.getLogger(__name__)


def _created_at_in_range(
    created_at: Optional[str], date_from: Optional[str], date_to: Optional[str]
) -> bool:
    """هل تاريخ إنشاء الطلب (ISO 8601) يقع بفترة [date_from, date_to]؟

    نتساهل عمداً مع أي خطأ تحليل (تنسيق غير متوقَّع من باك اند السستم
    الحقيقي، أو created_at مفقود): **نُبقي** الطلب بدل ما نخفيه — إخفاء طلب
    حقيقي عن الموظف بسبب فلتر تاريخ فشل تحليله أسوأ من عرضه بلا فلترة."""
    if not created_at:
        return True
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if date_from and created < datetime.fromisoformat(date_from):
            return False
        if date_to and created > datetime.fromisoformat(date_to + "T23:59:59"):
            return False
        return True
    except ValueError:
        return True


def _parse_orders(raw_orders: list) -> List[dict]:
    """يتحقق من كل طلب مقابل SystemOrder ويرجعه كـ dict. طلب لا يطابق
    العقد يُستبعَد بصمت مع تحذير باللوق — انظر تعليق الملف أعلاه."""
    parsed: List[dict] = []
    for item in raw_orders:
        try:
            parsed.append(SystemOrder.model_validate(item).model_dump())
        except ValidationError as exc:
            logger.warning("عنصر طلب لا يطابق عقد SystemOrder، تم استبعاده: %s", exc)
    return parsed

# ---------------------------------------------------------------------------
# إخراج (Outbound) — استعلام حالة طلب
# ---------------------------------------------------------------------------


class OrderStatusProvider(ABC):
    @abstractmethod
    async def get_by_order_id(self, order_id: str, api_key: str) -> Optional[dict]:
        """يرجع حالة طلب واحد بمعرّفه، أو None إذا غير موجود."""

    @abstractmethod
    async def search_by_phone(
        self,
        phone: str,
        api_key: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[dict]:
        """يرجع كل الطلبات المرتبطة برقم هاتف، بفلترة اختيارية بفترة تاريخ
        (ISO 8601، مثل "2026-07-01") — البحث الأساسي بميزة الدعم صار برقم
        الهاتف (الزبون يتذكره، لا رقم الطلب)، مع مرونة تحديد فترة زمنية
        («طلبات هذا الرقم من الشهر الماضي»)."""

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
        data = await self._get(f"/orders/{order_id}", api_key)
        if data is None:
            return None
        try:
            return SystemOrder.model_validate(data).model_dump()
        except ValidationError as exc:
            logger.warning("طلب %s لا يطابق عقد SystemOrder: %s", order_id, exc)
            return None

    async def search_by_phone(
        self,
        phone: str,
        api_key: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[dict]:
        # نمرر فترة التاريخ لباك اند السستم (تخمين موثَّق — TODO أعلى الملف)
        # **و**نفلتر محلياً كمان على created_at: باك اند السستم قد يتجاهل
        # فلاتر غير معروفة له، فالفلترة المحلية شبكة أمان تضمن النتيجة صحيحة
        # بغض النظر هل الباك اند الحقيقي يدعم هذا الفلتر أصلاً أم لا.
        params = {"phone": phone}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        data = await self._get("/orders/search", api_key, params=params)
        orders = _parse_orders((data or {}).get("orders", []))
        if date_from or date_to:
            orders = [
                o for o in orders
                if _created_at_in_range(o.get("created_at"), date_from, date_to)
            ]
        return orders

    async def search_by_status(self, status: str, api_key: str) -> List[dict]:
        data = await self._get("/orders/search", api_key, params={"status": status})
        return _parse_orders((data or {}).get("orders", []))

    async def list_all(self, api_key: str) -> List[dict]:
        data = await self._get("/orders", api_key)
        return _parse_orders((data or {}).get("orders", []))


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
