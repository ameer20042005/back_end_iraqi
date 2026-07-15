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


class OrderExtraction(BaseModel):
    """المخطط الذي يُطلب من الموديل تعبئته حرفياً (JSON فقط) عند تثبيت الطلب."""

    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None
    customer_address: Optional[str] = None
    items: List[OrderItemExtraction] = Field(default_factory=list)
    suggested_product_name: Optional[str] = None
    notes: Optional[str] = None
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
    customer_address: Optional[str] = None
    items: List[ResolvedOrderItem]
    suggested_product: Optional[dict] = None  # {id, name, price, currency} إن وُجد تطابق
    subtotal: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = None
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
