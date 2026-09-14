# -*- coding: utf-8 -*-
"""عقد الاستعلام (app/order_query.py) — المرحلة 1 من docs/fix-plan.md.

التشغيل:  python -m pytest tests/test_order_query.py -v
"""

import pytest
from pydantic import ValidationError

from app.order_query import OrderQuery, PagedOrders


def test_limit_above_cap_is_rejected():
    """خط الدفاع ضد عودة الحقن الضخم — الموديل يطلب 5000 صف، النموذج يرفض
    قبل ما يصل الطلب للمستودع أصلاً (le=50)."""
    with pytest.raises(ValidationError):
        OrderQuery(limit=5000)


def test_limit_zero_is_rejected():
    with pytest.raises(ValidationError):
        OrderQuery(limit=0)


def test_defaults_are_ten_rows_from_start():
    q = OrderQuery()
    assert (q.limit, q.offset) == (10, 0)


def test_unknown_parameter_is_rejected():
    """extra="forbid" — معامل مخترَع من الموديل ("all": true من البرومبت
    القديم مثلاً) يُرفض برسالة واضحة بدل ما يُتجاهَل صامتاً."""
    with pytest.raises(ValidationError):
        OrderQuery(all=True)


def test_has_more_when_page_ends_before_total():
    page = PagedOrders(orders=[{}] * 10, total=25, offset=0)
    assert page.has_more is True


def test_has_more_false_when_page_reaches_total():
    """الحالة الحدّية (offset + len == total) لازم ترجع False — وإلا طلب
    الموديل صفحة فارغة."""
    page = PagedOrders(orders=[{}] * 5, total=15, offset=10)
    assert page.has_more is False


def test_has_more_false_on_empty_result():
    assert PagedOrders(orders=[], total=0).has_more is False
