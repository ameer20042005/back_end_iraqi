# -*- coding: utf-8 -*-
"""صيغة تثبيت الطلب: ما يستخرجه الموديل من المحادثة، وما يرجعه الباك اند فعلياً.

الفصل بين OrderExtraction (خام من الموديل) وOrderConfirmation (بعد الحل مقابل
الكتالوج) مقصود: لا نثق بالموديل بخصوص الأسعار/المجموع — دائماً نحسبها من
app/products.py على الخادم.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


class OrderItemExtraction(BaseModel):
    product_name: str
    quantity: int = 1


class PlaneOrderItem(BaseModel):
    """عنصر orders بمخطط plane.md — quantity = 0 يعني كمية غير مؤكدة."""

    name: str = ""
    quantity: int = 0


class PlaneOrderExtraction(BaseModel):
    """مخطط استخراج طلب من رسالة عراقية خام (plane.md حرفياً) — يُستخدم
    كـ guided_json بمسار /orders/create ثم يُحوَّل إلى OrderExtraction
    عبر to_order_extraction() حتى يمر بنفس resolve_order الموثوق."""

    name: str = ""
    city: str = ""
    district: str = ""
    address: str = ""
    phone1: str = ""
    phone2: str = ""
    price: str = ""
    note: str = ""
    orders: List[PlaneOrderItem] = Field(default_factory=list)
    totalQuantity: int = 0

    def to_order_extraction(self) -> "OrderExtraction":
        full_address = " - ".join(p for p in (self.city, self.district, self.address) if p)
        return OrderExtraction(
            customer_name=self.name or None,
            customer_phone=self.phone1 or None,
            customer_phone2=self.phone2 or None,
            customer_address=full_address or None,
            customer_city=self.city or None,
            customer_district=self.district or None,
            # quantity = 0 بمخطط plane.md تعني "غير مؤكدة" — نحلّها ككمية 1
            # حتى يُحسب سطر المنتج بالكتالوج بدل سطر بمجموع صفري مضلِّل.
            items=[
                OrderItemExtraction(product_name=o.name, quantity=max(o.quantity, 1))
                for o in self.orders if o.name
            ],
            notes=self.note or None,
            quoted_price=self.price or None,
        )


class OrderExtraction(BaseModel):
    """المخطط الذي يُطلب من الموديل تعبئته حرفياً (JSON فقط) عند تثبيت الطلب."""

    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_phone2: Optional[str] = None
    customer_address: Optional[str] = None
    customer_city: Optional[str] = None       # المحافظة (تُصحَّح من app/rag/locations.py)
    customer_district: Optional[str] = None   # المنطقة/الحي كما وردت بالرسالة
    state_code: Optional[str] = None          # كود المحافظة بنظام شركة التوصيل (BGD...)
    items: List[OrderItemExtraction] = Field(default_factory=list)
    suggested_product_name: Optional[str] = None
    notes: Optional[str] = None
    quoted_price: Optional[str] = None  # السعر كما ورد بالرسالة — للاطلاع فقط، المجموع يُحسب من الكتالوج
    confirmation_note: Optional[str] = None  # جملة ودّية باللهجة العراقية، بدون أرقام/أسعار


class ResolvedOrderItem(BaseModel):
    product_id: Optional[str] = None
    product_name: str
    quantity: int
    unit_price: Optional[float] = None
    currency: Optional[str] = None
    line_total: Optional[float] = None
    matched: bool = False  # هل انطبق على منتج فعلي بالكتالوج


class OrderConfirmation(BaseModel):
    order_id: str
    created_at: str  # ISO 8601
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_phone2: Optional[str] = None
    customer_address: Optional[str] = None
    customer_city: Optional[str] = None
    customer_district: Optional[str] = None
    state_code: Optional[str] = None
    items: List[ResolvedOrderItem]
    suggested_product: Optional[dict] = None  # {id, name, price, currency} إن وُجد تطابق
    subtotal: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = None
    quoted_price: Optional[str] = None  # السعر المذكور برسالة الزبون كما هو — لا يدخل بحساب total
    notes: Optional[str] = None
    confirmation_message: str


def parse_order_extraction(raw: str) -> OrderExtraction:
    """يحوّل نص خام من الموديل إلى OrderExtraction، مع محاولة تنظيف بسيطة
    (أسوار Markdown، نص قبل/بعد الكائن) إذا فشل guided decoding أو لم يكن
    مدعوماً بمحرك التوليد الحالي. يرجع OrderExtraction فارغ عند فشل التحليل
    بدل رمي استثناء يكسر الطلب بالكامل."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        return OrderExtraction.model_validate_json(text)
    except Exception:
        return OrderExtraction(items=[])


def parse_plane_extraction(raw: str) -> Optional[PlaneOrderExtraction]:
    """نفس تنظيف parse_order_extraction لكن لمخطط plane.md — يرجع None عند
    فشل التحليل حتى يقرر المستدعي (router) مسار الاستخراج البدائي البديل."""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1:
        text = text[start:end + 1]
    try:
        return PlaneOrderExtraction.model_validate_json(text)
    except Exception:
        return None
