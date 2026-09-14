# -*- coding: utf-8 -*-
"""البحث المصفّح بالخطة البديلة (app/order_gateway.py::filter_orders_locally،
HttpOrderStatusProvider.search/count_for_query) — docs/fix-plan.md § المرحلة 2
وdocs/contract-matching.md: باك اند السستم الفعلي لا يدعم limit/offset، فالقصّ
والفلترة المركّبة يصيران داخل طبقة المستودع حصراً.

التشغيل:  python -m pytest tests/test_order_gateway_search.py -v
"""

import asyncio

from app.order_gateway import HttpOrderStatusProvider, filter_orders_locally
from app.order_query import OrderQuery

_ORDERS = [
    {"order_id": "ORD-1", "phone": "07701234567", "status": "قيد التوصيل",
     "customer_city": "بغداد", "customer_name": "سارة احمد", "created_at": "2026-08-01T10:00:00Z"},
    {"order_id": "ORD-2", "phone": "+964 770 123 4567", "status": "شحنات سلمت بنجاح",
     "customer_city": "البصرة", "customer_name": None, "created_at": "2026-08-15T10:00:00Z"},
    {"order_id": "ORD-3", "phone": "07809998877", "status": "قيد التوصيل",
     "customer_city": "بغداد", "customer_name": "علي", "created_at": "2026-09-01T10:00:00Z"},
]


# --------------------------------------------------------------------------
# filter_orders_locally
# --------------------------------------------------------------------------


def _ids(orders):
    return [o["order_id"] for o in orders]


def test_no_criteria_keeps_everything():
    assert _ids(filter_orders_locally(_ORDERS, OrderQuery())) == ["ORD-1", "ORD-2", "ORD-3"]


def test_status_matches_by_containment_after_normalize():
    """الموديل قد يرسل جزءاً من الحالة («توصيل») — الاحتواء بعد التطبيع."""
    assert _ids(filter_orders_locally(_ORDERS, OrderQuery(status="توصيل"))) == ["ORD-1", "ORD-3"]


def test_city_matches_with_different_hamza_forms():
    assert _ids(filter_orders_locally(_ORDERS, OrderQuery(city="بغداد"))) == ["ORD-1", "ORD-3"]
    assert _ids(filter_orders_locally(_ORDERS, OrderQuery(city="البصره"))) == ["ORD-2"]


def test_phone_matches_on_digits_only():
    """الخادم قد يرجّع الهاتف بفواصل أو بمقدمة دولية — نقارن الأرقام فقط."""
    assert _ids(filter_orders_locally(_ORDERS, OrderQuery(phone="+9647701234567"))) == ["ORD-1", "ORD-2"]


def test_customer_name_none_never_matches():
    """customer_name يصل None دائماً من jbot — لا يطابق، لكن لا ينكسر."""
    assert _ids(filter_orders_locally(_ORDERS, OrderQuery(customer_name="سارة"))) == ["ORD-1"]


def test_date_range_uses_created_at():
    q = OrderQuery(date_from="2026-08-10", date_to="2026-08-31")
    assert _ids(filter_orders_locally(_ORDERS, q)) == ["ORD-2"]


def test_combined_criteria_intersect():
    q = OrderQuery(status="قيد التوصيل", city="بغداد", date_from="2026-08-20")
    assert _ids(filter_orders_locally(_ORDERS, q)) == ["ORD-3"]


# --------------------------------------------------------------------------
# HttpOrderStatusProvider.search / count_for_query (بلا شبكة — نبدّل الجالبين)
# --------------------------------------------------------------------------


class _Spy:
    def __init__(self):
        self.calls = []


def _provider(monkeypatch, server_count=None):
    provider = HttpOrderStatusProvider(base_url="http://backend.test")
    spy = _Spy()

    async def list_all(api_key):
        spy.calls.append("list_all")
        return list(_ORDERS)

    async def search_by_status(status, api_key):
        spy.calls.append(("search_by_status", status))
        return [o for o in _ORDERS if status in o["status"]]

    async def search_by_phone(phone, api_key, date_from=None, date_to=None):
        spy.calls.append(("search_by_phone", phone))
        return [o for o in _ORDERS if o["phone"] == phone]

    async def get_by_order_id(order_id, api_key):
        spy.calls.append(("get_by_order_id", order_id))
        return next((o for o in _ORDERS if o["order_id"] == order_id), None)

    async def count(api_key, phone=None, status=None, date_from=None, date_to=None):
        spy.calls.append(("count", status))
        return server_count

    monkeypatch.setattr(provider, "list_all", list_all)
    monkeypatch.setattr(provider, "search_by_status", search_by_status)
    monkeypatch.setattr(provider, "search_by_phone", search_by_phone)
    monkeypatch.setattr(provider, "get_by_order_id", get_by_order_id)
    monkeypatch.setattr(provider, "count", count)
    return provider, spy


def test_search_narrows_at_server_before_local_filter(monkeypatch):
    """سؤال بحالة يستعمل /orders/search?status لا الدفتر كله."""
    provider, spy = _provider(monkeypatch)
    page = asyncio.run(provider.search(OrderQuery(status="قيد التوصيل", city="بغداد"), "k"))
    assert spy.calls == [("search_by_status", "قيد التوصيل")]
    assert _ids(page.orders) == ["ORD-1", "ORD-3"]
    assert page.total == 2 and page.has_more is False


def test_search_pages_and_reports_total(monkeypatch):
    """total يُحسب قبل القصّ — الموديل يعرف أن هناك المزيد (العطل B1)."""
    provider, _spy = _provider(monkeypatch)
    page = asyncio.run(provider.search(OrderQuery(limit=2), "k"))
    assert _ids(page.orders) == ["ORD-1", "ORD-2"]
    assert page.total == 3 and page.has_more is True
    page2 = asyncio.run(provider.search(OrderQuery(limit=2, offset=2), "k"))
    assert _ids(page2.orders) == ["ORD-3"] and page2.has_more is False


def test_search_by_order_id_fetches_single_order(monkeypatch):
    provider, spy = _provider(monkeypatch)
    page = asyncio.run(provider.search(OrderQuery(order_id="ORD-2"), "k"))
    assert spy.calls == [("get_by_order_id", "ORD-2")]
    assert page.total == 1


def test_count_for_query_prefers_server_count(monkeypatch):
    """فلاتر يفهمها الخادم → COUNT بجهته، بلا جلب أي صف."""
    provider, spy = _provider(monkeypatch, server_count=340)
    total = asyncio.run(provider.count_for_query(OrderQuery(status="قيد التوصيل"), "k"))
    assert total == 340
    assert spy.calls == [("count", "قيد التوصيل")]


def test_count_for_query_falls_back_when_server_cannot(monkeypatch):
    """فلتر المدينة لا يفهمه الخادم → عدّ محلي من search().total.
    ورد عدّ فاشل (None) → نفس السقوط بدل رقم مخترَع."""
    provider, spy = _provider(monkeypatch, server_count=None)
    assert asyncio.run(provider.count_for_query(OrderQuery(city="بغداد"), "k")) == 2
    assert not any(c[0] == "count" for c in spy.calls)
    assert asyncio.run(provider.count_for_query(OrderQuery(status="توصيل"), "k")) == 2
