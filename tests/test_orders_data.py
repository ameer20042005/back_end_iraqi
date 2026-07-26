# -*- coding: utf-8 -*-
"""فحص مصدر بيانات الطلبات (app/data/orders.json + StaticOrderStatusProvider).

الغاية: تثبيت **عقد البيانات**. كل حقل يفحصه هذا الملف هو حقل يعتمد عليه
الراوتر فعلاً ببناء رد الزبون (_format_order_reply). إذا الـ API الحقيقي رجّع
شكلاً مختلفاً، هذي الاختبارات تكسر فوراً بدل ما ينكسر الرد بوجه الزبون.

التشغيل:  python -m pytest tests/test_orders_data.py -v
"""

import asyncio
import json
import os

import pytest

from app.order_gateway import (
    DEFAULT_ORDERS_PATH,
    StaticOrderStatusProvider,
    order_status_provider,
)

with open(DEFAULT_ORDERS_PATH, encoding="utf-8") as f:
    RAW_ORDERS = json.load(f)

REQUIRED_FIELDS = ("order_id", "phone", "status", "items", "eta")


def test_orders_file_lives_in_data_folder():
    """الطلبات بملف بيانات جنب المنتجات — مو مضمّنة بالكود."""
    assert os.path.basename(DEFAULT_ORDERS_PATH) == "orders.json"
    assert os.path.basename(os.path.dirname(DEFAULT_ORDERS_PATH)) == "data"
    assert os.path.exists(DEFAULT_ORDERS_PATH)


@pytest.mark.parametrize("order", RAW_ORDERS, ids=[o["order_id"] for o in RAW_ORDERS])
def test_order_record_matches_contract(order):
    """كل سجل: الحقول الخمسة موجودة، والأنواع مطابقة لما يتوقعه الراوتر."""
    for field in REQUIRED_FIELDS:
        assert field in order, f"حقل ناقص: {field}"

    assert order["order_id"].startswith("ORD-")
    assert order["phone"].startswith("07") and len(order["phone"]) == 11
    assert isinstance(order["status"], str) and order["status"]
    assert order["eta"] is None or isinstance(order["eta"], str)

    assert isinstance(order["items"], list) and order["items"], "الطلب بلا أصناف"
    for item in order["items"]:
        assert isinstance(item["product_name"], str) and item["product_name"]
        assert isinstance(item["quantity"], int) and item["quantity"] >= 1


def test_order_ids_are_unique():
    """معرّف الطلب مفتاح — التكرار يخلي الفهرس يرجّع سجلاً واحداً بصمت."""
    ids = [o["order_id"] for o in RAW_ORDERS]
    assert len(ids) == len(set(ids))


def test_provider_loads_every_order_from_the_file():
    """المزوّد يقرأ الملف كاملاً — ما يفوت أي سجل."""
    assert len(order_status_provider.orders) == len(RAW_ORDERS)


def test_lookup_by_order_id_is_case_insensitive():
    """«ord-1001» من الزبون لازم توصل لنفس سجل «ORD-1001»."""
    upper = asyncio.run(order_status_provider.get_by_order_id("ORD-1001"))
    lower = asyncio.run(order_status_provider.get_by_order_id("ord-1001"))
    assert upper is not None and upper is lower


def test_lookup_by_phone_returns_all_matching_orders():
    """رقم عليه طلبان يرجّع الاثنين."""
    orders = asyncio.run(order_status_provider.search_by_phone("07512223344"))
    assert {o["order_id"] for o in orders} == {"ORD-1003", "ORD-1004"}


def test_unknown_lookups_return_empty_not_error():
    assert asyncio.run(order_status_provider.get_by_order_id("ORD-9999")) is None
    assert asyncio.run(order_status_provider.search_by_phone("07700000000")) == []


def test_phone_result_is_a_copy():
    """تعديل القائمة المُرجَعة ما يلوّث فهرس المزوّد."""
    first = asyncio.run(order_status_provider.search_by_phone("07512223344"))
    first.clear()
    second = asyncio.run(order_status_provider.search_by_phone("07512223344"))
    assert len(second) == 2


def test_missing_file_fails_loudly():
    """ملف مفقود = خطأ صريح عند الإقلاع، مو مزوّد فارغ يرد «ماكو طلبات» لكل زبون."""
    with pytest.raises(FileNotFoundError):
        StaticOrderStatusProvider(orders_path="ماكو_هيچ_ملف.json")
