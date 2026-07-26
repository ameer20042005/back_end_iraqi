# -*- coding: utf-8 -*-
"""بوابة نظام إدارة الطلبات الخارجي — واجهتان منفصلتان لكل اتجاه بيانات:

- **إخراج (Outbound)** — `OrderStatusProvider`: نستعلم منه (تتبع حالة طلب
  برقم الطلب أو الهاتف). يستخدمها `app/features/support/`.
- **إدخال (Inbound)** — `OrderSubmitter`: نرسل له طلباً جديداً بعد ما تحسبه
  `app/features/sales/service.py` (أو `order_intake`). بدونها، الطلبات
  المؤكَّدة كانت تُرجَع بالـ API response بس وما توصل أي نظام خارجي فعلي.

نفس نمط app/products.py: واجهات مجرّدة + تطبيقات محلية الآن، تُستبدل لاحقاً
بعميل API حقيقي بدون تغيير أي كود مستدعي — لا مصادقة مطلوبة (API مخصص لمهمة
تتبع/تثبيت الطلبات فقط، حسب توضيح المستخدم)، فقط:

    class HttpOrderStatusProvider(OrderStatusProvider):
        def __init__(self, base_url: str): ...
        async def get_by_order_id(self, order_id): ...
        async def search_by_phone(self, phone): ...

    class HttpOrderSubmitter(OrderSubmitter):
        def __init__(self, base_url: str): ...
        async def submit(self, order): ...

مصدر البيانات الآن:
- الطلبات (إخراج) — `app/data/orders.json`، يقرأها `StaticOrderStatusProvider`.
  شكل السجل بذاك الملف هو **عقد البيانات** المتوقع من النظام الحقيقي:
  order_id, phone, status, items[{product_name, quantity}], eta.

  ملاحظة مقصودة: أسماء المنتجات بالطلبات **مستقلة** عن كتالوج
  `app/data/products.json` — بعض الأصناف بالطلبات التجريبية ما موجودة
  بالكتالوج، وهذا هو السلوك الصحيح: الطلب سجل تاريخي يحمل اسم المنتج وقت
  الشراء، وقد يكون الصنف انسحب من العرض بعدين. لذلك `_format_order_reply`
  تقرأ `product_name` من سجل الطلب مباشرة ولا تستشير الكتالوج أبداً.
- الإرسال (إدخال) — بالذاكرة فقط (`MockOrderSubmitter`)، بلا ملف: الطلبات
  المُرسَلة مخرجات مو بيانات تجريبية نحرّرها بيد.

TODO: استبدل التطبيقين أدناه بعميل API حقيقي عند توفر تفاصيل الاتصال (رابط كل
عملية بالضبط، وشكل الاستجابة).
"""

import json
import os
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Dict, List, Optional

from app.order_schema import OrderConfirmation

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ORDERS_PATH = os.path.join(BASE_DIR, "data", "orders.json")

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


class StaticOrderStatusProvider(OrderStatusProvider):
    """طلبات تجريبية من ملف JSON محلي (app/data/orders.json) — نفس نمط
    `StaticProductRepository` بـ app/products.py.

    البيانات بملف بيانات مو بالكود عمداً: تعديل الطلبات التجريبية أو توسعتها
    ما يحتاج لمس أي بايثون، وشكل السجل هنا هو نفسه العقد اللي لازم يرجّعه
    الـ API الحقيقي — فاستبداله لاحقاً بـ`HttpOrderStatusProvider` يصير تبديل
    مصدر فقط، بلا تغيير بالراوتر ولا بصيغة الرد.

    الفهرسة بالذاكرة (order_id، phone) بدل المسح الخطي: نفس شكل الاستعلام
    اللي راح ينفّذه الباك اند الحقيقي (WHERE order_id = ... / WHERE phone = ...).
    """

    def __init__(self, orders_path: str = DEFAULT_ORDERS_PATH):
        self.orders: List[dict] = []
        self._by_order_id: Dict[str, dict] = {}
        self._by_phone: Dict[str, List[dict]] = defaultdict(list)
        self._load(orders_path)

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            raise FileNotFoundError(f"ملف الطلبات غير موجود: {path}")
        with open(path, encoding="utf-8") as f:
            self.orders = json.load(f)

        for order in self.orders:
            self._by_order_id[str(order["order_id"]).upper()] = order
            self._by_phone[str(order["phone"])].append(order)

    async def get_by_order_id(self, order_id: str) -> Optional[dict]:
        return self._by_order_id.get(str(order_id).upper())

    async def search_by_phone(self, phone: str) -> List[dict]:
        return list(self._by_phone.get(str(phone), []))


order_status_provider: OrderStatusProvider = StaticOrderStatusProvider()


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
