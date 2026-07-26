# -*- coding: utf-8 -*-
"""فحص حارس الإرسال لنظام الطلبات (app/features/sales/service.py).

بوابة الاكتمال بالراوتر تحرس **المدخل** (هل ذكر العميل بياناته؟)، لكن بينها
وبين الإرسال تجري خطوة استخراج بالموديل ممكن تفشل — وعندها كان يُبنى طلب من
آخر رسالة عميل («نعم») ويُرسل لنظام الطلبات كأنه صحيح. الحارس هنا آخر خط.

يغطي مسارات الطلب كلها لأنها كلها تمر بـ resolve_order:
/sales/chat و/orders/create (نص، صوت، صورة).

التشغيل:  python -m pytest tests/test_order_submission_guard.py -v
"""

import asyncio

import pytest

from app.features.sales.service import _submission_blockers, resolve_order
from app.order_gateway import order_submitter
from app.order_schema import OrderExtraction, OrderItemExtraction

_COMPLETE = dict(
    customer_name="امير وسام",
    customer_phone="07712345678",
    customer_city="بغداد",
    customer_district="الحارثية",
    items=[OrderItemExtraction(product_name="ماوس لاسلكي لوجيتك", quantity=1)],
)


@pytest.fixture(autouse=True)
def clear_submitted():
    order_submitter.submitted.clear()
    yield
    order_submitter.submitted.clear()


def test_complete_order_is_submitted():
    """الطلب المكتمل لازم يوصل نظام الطلبات — حجبه هنا يعني ضياع بيع صحيح."""
    order = asyncio.run(resolve_order(OrderExtraction(**_COMPLETE)))
    assert _submission_blockers(order) == []
    assert len(order_submitter.submitted) == 1


def test_fallback_confirmation_word_order_is_not_submitted():
    """عطل الإنتاج حرفياً: فشل الاستخراج فصار «نعم» اسم المنتج."""
    order = asyncio.run(resolve_order(
        OrderExtraction(items=[OrderItemExtraction(product_name="نعم", quantity=1)])
    ))
    blockers = _submission_blockers(order)
    assert "items" in blockers and "phone" in blockers
    assert order_submitter.submitted == []


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("customer_phone", None, "phone"),
        ("customer_phone", "123", "phone"),          # مو رقم عراقي صالح
        ("customer_phone", "0771234567", "phone"),   # 10 خانات لا 11
        ("customer_name", None, "name"),
        ("customer_name", "   ", "name"),
    ],
)
def test_incomplete_field_blocks_submission(field, value, expected):
    data = dict(_COMPLETE)
    data[field] = value
    order = asyncio.run(resolve_order(OrderExtraction(**data)))
    assert expected in _submission_blockers(order)
    assert order_submitter.submitted == []


def test_missing_location_blocks_submission():
    """بلا محافظة ولا منطقة ولا عنوان حر ماكو وين نوصّل."""
    data = dict(_COMPLETE)
    data["customer_city"] = data["customer_district"] = None
    order = asyncio.run(resolve_order(OrderExtraction(**data)))
    assert "location" in _submission_blockers(order)
    assert order_submitter.submitted == []


def test_free_address_alone_is_enough_location():
    """عنوان حر بلا محافظة مصنّفة يكفي — لا نحجب طلباً عنوانه معروف."""
    data = dict(_COMPLETE)
    data["customer_city"] = data["customer_district"] = None
    data["customer_address"] = "شارع فلسطين، قرب الجامع"
    order = asyncio.run(resolve_order(OrderExtraction(**data)))
    assert "location" not in _submission_blockers(order)
    assert len(order_submitter.submitted) == 1


def test_empty_items_blocks_submission():
    """طلب بلا أي صنف — يصير عند فشل قراءة صورة أو صوت بـ /orders/create."""
    data = dict(_COMPLETE)
    data["items"] = []
    order = asyncio.run(resolve_order(OrderExtraction(**data)))
    assert "items" in _submission_blockers(order)
    assert order_submitter.submitted == []


def test_uncatalogued_product_still_submitted():
    """منتج خارج الكتالوج طلب صالح (انظر رأس service.py) — ما ينحجب."""
    data = dict(_COMPLETE)
    data["items"] = [OrderItemExtraction(product_name="طرشي اصفر 1 ك", quantity=2)]
    order = asyncio.run(resolve_order(OrderExtraction(**data)))
    assert _submission_blockers(order) == []
    assert len(order_submitter.submitted) == 1
