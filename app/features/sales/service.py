# -*- coding: utf-8 -*-
"""يحوّل OrderExtraction (خام من الموديل) إلى OrderConfirmation موثوق —
الأسعار والمجموع تُحسب هنا من app/products.py دائماً، لا نثق بأرقام الموديل."""

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from app.order_gateway import order_submitter
from app.order_schema import OrderConfirmation, OrderExtraction, ResolvedOrderItem
from app.products import ProductRepository, product_repository


def _resolve_product(repo: ProductRepository, name: str) -> Optional[dict]:
    matches = repo.search(name, top_k=1)
    return matches[0] if matches else None


async def resolve_order(
    extraction: OrderExtraction,
    repo: ProductRepository = product_repository,
) -> OrderConfirmation:
    resolved_items = []
    subtotal = 0.0
    all_matched = True
    currency = None

    for item in extraction.items:
        product = _resolve_product(repo, item.product_name)
        if product:
            line_total = product["price"] * item.quantity
            subtotal += line_total
            currency = currency or product.get("currency")
            resolved_items.append(ResolvedOrderItem(
                product_id=str(product["id"]),
                product_name=product["name"],
                quantity=item.quantity,
                unit_price=product["price"],
                currency=product.get("currency"),
                line_total=line_total,
                matched=True,
            ))
        else:
            all_matched = False
            resolved_items.append(ResolvedOrderItem(
                product_name=item.product_name,
                quantity=item.quantity,
                matched=False,
            ))

    suggested_product = None
    if extraction.suggested_product_name:
        match = _resolve_product(repo, extraction.suggested_product_name)
        if match:
            suggested_product = {
                "id": str(match["id"]),
                "name": match["name"],
                "price": match["price"],
                "currency": match.get("currency"),
            }

    # مخطط الاستخراج يمنع الأرقام برسالة التأكيد (الأسعار تُحسب هنا من
    # الكتالوج فقط) — لو الموديل خالف وذكر رقماً، نتجاهل رسالته كلها ونستخدم
    # الرسالة الافتراضية بدل تمرير رقم مختلَق للعميل.
    note = extraction.confirmation_note
    if note and re.search(r"\d", note):
        note = None
    note = note or "تم تثبيت طلبك، وياتك بأقرب وقت ان شاء الله."
    if not all_matched:
        note += " (تنبيه: بعض المنتجات المطلوبة ما انطبقت على الكتالوج الحالي وتحتاج مراجعة يدوية.)"

    confirmation = OrderConfirmation(
        order_id=str(uuid.uuid4()),
        created_at=datetime.now(timezone.utc).isoformat(),
        customer_name=extraction.customer_name,
        customer_phone=extraction.customer_phone,
        customer_phone2=extraction.customer_phone2,
        customer_address=extraction.customer_address,
        customer_city=extraction.customer_city,
        customer_district=extraction.customer_district,
        state_code=extraction.state_code,
        items=resolved_items,
        suggested_product=suggested_product,
        subtotal=subtotal if resolved_items else None,
        total=subtotal if resolved_items else None,
        currency=currency,
        quoted_price=extraction.quoted_price,
        notes=extraction.notes,
        confirmation_message=note,
    )

    # يرسل الطلب المؤكَّد لنظام إدارة الطلبات الخارجي (Mock حالياً — انظر
    # app/order_gateway.py). لا نفشل تسليم الرد للعميل لو تعذّر الإرسال؛ الطلب
    # يبقى موجوداً بالرد على أي حال ويمكن إعادة محاولة إرساله لاحقاً.
    try:
        await order_submitter.submit(confirmation)
    except Exception:
        pass

    return confirmation
