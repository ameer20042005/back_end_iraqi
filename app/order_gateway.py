# -*- coding: utf-8 -*-
"""بوابة نظام إدارة الطلبات الخارجي — واجهتان منفصلتان لكل اتجاه بيانات:

- **إخراج (Outbound)** — `OrderStatusProvider`: نستعلم منه (تتبع حالة طلب
  برقم الطلب أو الهاتف). يستخدمها `app/features/support/`.
- **إدخال (Inbound)** — `OrderSubmitter`: نرسل له طلباً جديداً بعد ما تحسبه
  `app/features/sales/service.py` (أو `order_intake`). بدونها، الطلبات
  المؤكَّدة كانت تُرجَع بالـ API response بس وما توصل أي نظام خارجي فعلي.

نفس نمط app/products.py: واجهات مجرّدة + تطبيقات mock الآن، تُستبدل لاحقاً
بعميل API حقيقي بدون تغيير أي كود مستدعي — لا مصادقة مطلوبة (API مخصص لمهمة
تتبع/تثبيت الطلبات فقط، حسب توضيح المستخدم)، فقط:

    class HttpOrderStatusProvider(OrderStatusProvider):
        def __init__(self, base_url: str): ...
        async def get_by_order_id(self, order_id): ...
        async def search_by_phone(self, phone): ...

    class HttpOrderSubmitter(OrderSubmitter):
        def __init__(self, base_url: str): ...
        async def submit(self, order): ...

TODO: استبدل الـ Mock أدناه بعميل API حقيقي عند توفر تفاصيل الاتصال (رابط كل
عملية بالضبط، وشكل الاستجابة).
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from app.order_schema import OrderConfirmation

# ---------------------------------------------------------------------------
# إخراج (Outbound) — استعلام حالة طلب
# ---------------------------------------------------------------------------


class OrderStatusProvider(ABC):
    @abstractmethod
    async def get_by_order_id(self, order_id: str) -> Optional[dict]:
        """يرجع حالة طلب واحد بمعرّفه، أو None إذا غير موجود."""

    @abstractmethod
    async def search_by_phone(self, phone: str) -> List[dict]:
        """يرجع كل الطلبات المرتبطة برقم هاتف."""


class MockOrderStatusProvider(OrderStatusProvider):
    """بيانات تجريبية ثابتة لاختبار ميزة الدعم قبل ربط النظام الحقيقي."""

    _MOCK_ORDERS = [
        {
            "order_id": "ORD-1001",
            "phone": "07701234567",
            "status": "قيد التوصيل",
            "items": [{"product_name": "لابتوب لينوفو IdeaPad 15", "quantity": 1}],
            "eta": "خلال يومين",
        },
        {
            "order_id": "ORD-1002",
            "phone": "07709876543",
            "status": "تم التسليم",
            "items": [{"product_name": "سماعة بلوتوث JBL", "quantity": 2}],
            "eta": None,
        },
    ]

    async def get_by_order_id(self, order_id: str) -> Optional[dict]:
        return next((o for o in self._MOCK_ORDERS if o["order_id"] == order_id), None)

    async def search_by_phone(self, phone: str) -> List[dict]:
        return [o for o in self._MOCK_ORDERS if o["phone"] == phone]


order_status_provider: OrderStatusProvider = MockOrderStatusProvider()


# ---------------------------------------------------------------------------
# إدخال (Inbound) — تثبيت طلب جديد بالنظام الخارجي
# ---------------------------------------------------------------------------


class OrderSubmitter(ABC):
    @abstractmethod
    async def submit(self, order: OrderConfirmation) -> bool:
        """يرسل طلباً مؤكَّداً للنظام الخارجي. يرجع True لو نجح الإرسال."""


class MockOrderSubmitter(OrderSubmitter):
    """لا يرسل لأي نظام حقيقي بعد — يحتفظ بالطلبات بالذاكرة فقط لأغراض
    الاختبار المحلي، حتى تتوفر تفاصيل الـ API الحقيقي."""

    def __init__(self):
        self.submitted: List[OrderConfirmation] = []

    async def submit(self, order: OrderConfirmation) -> bool:
        self.submitted.append(order)
        return True


order_submitter: OrderSubmitter = MockOrderSubmitter()
