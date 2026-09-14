# -*- coding: utf-8 -*-
"""معايير البحث بدفتر الطلبات — العقد المشترك بين أداة الموديل
(app/features/support/router.py::_get_order_status_tool)، والمستودع
(app/order_gateway.py::OrderStatusProvider)، وباك اند السستم.

ليش ملف مستقل: هذا هو **الاسم الوحيد** الذي يعرفه الراوتر والبرومبت عن
معايير البحث. أسماء معاملات الخادم الفعلية (per_page، stepName، …) تبقى
حبيسة HttpOrderStatusProvider — لو تغيّر الخادم ما يتغيّر شي هنا ولا فوقه.
انظر docs/fix-plan.md § المرحلة 1 وdocs/contract-matching.md."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class OrderQuery(BaseModel):
    """نموذج معايير البحث. كل حقل هنا = بُعد واحد يقدر الموديل يضيّق فيه.

    سبب استخدام Pydantic لا dict خام: الموديل يرسل args عشوائية (يخترع أسماء
    معاملات أحياناً، مثل "all" أو "customer") — التحقق هنا يرفضها مبكراً بخطأ
    مفهوم بدل ما تمر صامتة وتُتجاهَل، فيظن الموديل أن فلتره طُبِّق وهو ما طُبِّق.
    extra="forbid" هو ما يجعل هذا الرفض فعلياً: افتراضي Pydantic يتجاهل الحقول
    الزائدة بصمت، وهذا بالضبط الصمت الذي نريد كسره.

    limit مسقَّف بـ50 بمستوى النموذج نفسه (le=50): حتى لو الموديل طلب 5000،
    التحقق يرفض — هذا خط الدفاع الذي يمنع عودة عطل الحقن الضخم (B1)."""

    model_config = ConfigDict(extra="forbid")

    order_id: Optional[str] = None
    phone: Optional[str] = None
    customer_name: Optional[str] = None
    status: Optional[str] = None
    city: Optional[str] = None
    date_from: Optional[str] = None  # ISO "YYYY-MM-DD"
    date_to: Optional[str] = None
    limit: int = Field(default=10, ge=1, le=50)
    offset: int = Field(default=0, ge=0)


class PagedOrders(BaseModel):
    """غلاف نتيجة مصفّحة. الحقل الحاسم هو `total` — العدد الكلي للمطابقات
    بالخادم، لا عدد ما رجع. بدونه الموديل ما عنده أي وسيلة يعرف أن هناك
    المزيد، فيتصرف وكأن العشرة التي وصلته هي كل شيء — وهذا بالضبط العطل B1.

    `has_more` مشتقة لا مخزَّنة: تسهّل على صائغ النص لاحقاً بلا حساب متكرر،
    ولا يمكن أن تتعارض مع total/offset لأنها تُحسب منهما دائماً."""

    orders: List[dict]
    total: int
    offset: int = 0

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.orders) < self.total
