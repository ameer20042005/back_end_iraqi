# -*- coding: utf-8 -*-
"""بناء مقاطع الحقن المشتركة (app/context_blocks.py) — سقف الحقن العام
(cap_for_model) وكتلتا الكتالوج/دفتر الطلبات الكاملتين.

الخلفية: next.md — الكتالوج/دفتر الطلبات يُحمَّلان كاملَين مرة وحدة بالجلسة
ويُحقنان بكل رسالة لاحقة (بدل بحث ضيّق لكل عنصر)، لكن الحقن يتنافس على نفس
ميزانية max_model_len مع البرومبت وتاريخ المحادثة — cap_for_model يحمي هذي
الميزانية بقصّ لحظي وقت التسليم للموديل، بلا مساس بالكاش الكامل وراءه.

التشغيل:  python -m pytest tests/test_context_blocks.py -v
"""

import logging

from app.context_blocks import cap_for_model, catalog_context_block, orders_context_block


def test_cap_for_model_keeps_items_within_limit():
    items = [{"id": str(i)} for i in range(5)]
    assert cap_for_model(items, max_items=10, label="t") == items


def test_cap_for_model_truncates_and_warns(caplog):
    items = [{"id": str(i)} for i in range(10)]
    with caplog.at_level(logging.WARNING):
        capped = cap_for_model(items, max_items=3, label="test-label")
    assert capped == items[:3]
    assert any("test-label" in r.message for r in caplog.records)


def test_cap_for_model_empty_list_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        capped = cap_for_model([], max_items=5, label="t")
    assert capped == []
    assert not caplog.records


def test_catalog_context_block_empty_returns_empty_string():
    assert catalog_context_block([]) == ""


def test_catalog_context_block_contains_product_json():
    block = catalog_context_block([{"id": "1", "name": "غسالة اتوماتيك", "price": 500000}])
    assert "غسالة اتوماتيك" in block
    assert "500000" in block
    assert "search_products" in block  # تعليمة "بلا حاجة تستدعي search_products ثانية"


def test_orders_context_block_empty_returns_empty_string():
    assert orders_context_block([]) == ""


def test_orders_context_block_contains_order_json():
    block = orders_context_block([{"order_id": "ORD-1001", "customer_name": "احمد"}])
    assert "ORD-1001" in block
    assert "احمد" in block
    assert "get_order_status" in block
