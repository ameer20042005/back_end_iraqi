# -*- coding: utf-8 -*-
"""أداة الموديل get_order_status/count_orders بعد إعادة الكتابة
(app/features/support/router.py — docs/fix-plan.md § المرحلة 3 و5):
معاملات مركّبة عبر OrderQuery، رد مصفّح بـ total/showing/hint، ورفض
المعاملات المخترَعة، وكاش النتائج بحدود الجلسة.

التشغيل:  python -m pytest tests/test_support_tool.py -v
"""

import asyncio
import uuid

import pytest

from app.features.support import router as support_router
from app.features.support.router import _count_orders_tool, _get_order_status_tool
from app.order_gateway import OrderStatusProvider, filter_orders_locally
from app.order_query import PagedOrders

_API_KEY = "k"


class _FakeProvider(OrderStatusProvider):
    def __init__(self, orders):
        self.orders = orders
        self.search_calls = 0

    async def get_by_order_id(self, order_id, api_key):
        return next((o for o in self.orders if o["order_id"] == order_id), None)

    async def search_by_phone(self, phone, api_key, date_from=None, date_to=None):
        return [o for o in self.orders if o["phone"] == phone]

    async def search_by_status(self, status, api_key):
        return [o for o in self.orders if o["status"] == status]

    async def list_all(self, api_key):
        return list(self.orders)

    async def search(self, query, api_key):
        self.search_calls += 1
        matched = filter_orders_locally(self.orders, query)
        return PagedOrders(
            orders=matched[query.offset: query.offset + query.limit],
            total=len(matched), offset=query.offset,
        )

    async def count(self, api_key, phone=None, status=None, date_from=None, date_to=None):
        return None

    async def get_history(self, order_id, api_key):
        return None


def _orders(n, status="قيد التوصيل"):
    return [
        {"order_id": f"ORD-{i:04d}", "phone": "07700000000", "status": status, "customer_city": "بغداد"}
        for i in range(n)
    ]


@pytest.fixture
def provider(monkeypatch):
    fake = _FakeProvider(_orders(25))
    monkeypatch.setattr(support_router, "order_status_provider", fake)
    return fake


def _sid():
    return f"support:test-{uuid.uuid4()}"


def _tool(args, sid=""):
    return asyncio.run(_get_order_status_tool(args, _API_KEY, session_id=sid))


def test_invented_parameter_is_rejected_with_arabic_error(provider):
    """"all": true تُهمَل بتسامح، لكن معاملاً مخترَعاً آخر يُرفض برسالة
    مفهومة للموديل لا stack trace."""
    result = _tool({"customer": "سارة"})
    assert "معاملات غير صالحة" in result["error"]
    assert "phone" in result["error"]
    assert provider.search_calls == 0


def test_limit_above_cap_is_rejected(provider):
    assert "error" in _tool({"limit": 5000})


def test_no_match_returns_found_false(provider):
    result = _tool({"status": "ملغي"})
    assert result["found"] is False and result["total"] == 0


def test_page_reports_total_and_hint_when_more_exist(provider):
    """الإصلاح الجوهري: showing/total/hint — الموديل يعرف أن 10 من أصل 25،
    ويُقال له صراحةً كيف يجيب الباقي (offset)."""
    result = _tool({"status": "قيد التوصيل", "city": "بغداد"})
    assert result["found"] is True
    assert result["showing"] == 10 and result["total"] == 25
    assert "من أصل 25" in result["hint"] and "offset=10" in result["hint"]


def test_last_page_has_no_hint(provider):
    result = _tool({"offset": 20})
    assert result["showing"] == 5 and "hint" not in result


def test_same_query_within_session_is_served_from_cache(provider):
    """نفس المعايير (حتى بترتيب مختلف) خلال الدقيقة → استعلام واحد فقط."""
    sid = _sid()
    _tool({"status": "قيد التوصيل", "limit": 5}, sid)
    _tool({"limit": 5, "status": "قيد التوصيل"}, sid)
    assert provider.search_calls == 1
    _tool({"status": "قيد التوصيل", "limit": 6}, sid)
    assert provider.search_calls == 2


def test_count_tool_returns_only_a_number(provider):
    result = asyncio.run(_count_orders_tool({"status": "قيد التوصيل"}, _API_KEY))
    assert result == {"count": 25}


def test_count_tool_rejects_unknown_args(provider):
    result = asyncio.run(_count_orders_tool({"foo": 1}, _API_KEY))
    assert "error" in result
