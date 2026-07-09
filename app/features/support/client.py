# -*- coding: utf-8 -*-
"""مصدر بيانات تتبع الطلبات — واجهة قابلة للربط بأي نظام خارجي حقيقي.

نفس نمط app/products.py: واجهة مجرّدة + تطبيق mock الآن، يُستبدل لاحقاً بعميل
API حقيقي (base URL + مصادقة) بدون تغيير أي كود مستدعي، مثلاً:

    class HttpOrderStatusProvider(OrderStatusProvider):
        def __init__(self, base_url: str, api_key: str): ...
        async def get_by_order_id(self, order_id): ...
        async def search_by_phone(self, phone): ...

    order_status_provider = HttpOrderStatusProvider(base_url=..., api_key=...)

TODO: استبدل MockOrderStatusProvider أدناه بـ API تتبع الطلبات الحقيقي عند
توفر تفاصيل الاتصال (base URL، طريقة المصادقة، شكل الاستجابة).
"""

from abc import ABC, abstractmethod
from typing import List, Optional


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
