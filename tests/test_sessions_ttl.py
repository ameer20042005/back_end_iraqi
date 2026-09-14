# -*- coding: utf-8 -*-
"""كاشات الجلسة بـTTL وسقف (app/sessions.py — docs/fix-plan.md § المرحلة 5):
كاش نتائج الاستعلامات (60 ثانية، 16 استعلاماً LRU)، كاش الكتالوج (300
ثانية — العطل B3)، وكاش الدفتر لاشتقاق الحالات (60 ثانية — العطل B4).

التشغيل:  python -m pytest tests/test_sessions_ttl.py -v
"""

import uuid

import pytest

from app import sessions
from app.order_query import OrderQuery


@pytest.fixture
def clock(monkeypatch):
    """ساعة وهمية بدل time.monotonic حتى نقدّم الوقت بلا انتظار فعلي."""
    state = {"now": 1000.0}
    monkeypatch.setattr(sessions.time, "monotonic", lambda: state["now"])
    return state


def _sid():
    return f"test-ttl-{uuid.uuid4()}"


def test_query_cache_key_is_order_independent():
    a = sessions.query_cache_key(OrderQuery(status="قيد التوصيل", city="بغداد"))
    b = sessions.query_cache_key(OrderQuery(city="بغداد", status="قيد التوصيل"))
    assert a == b
    assert "phone" not in a  # exclude_none — الفلاتر الغائبة لا تدخل المفتاح


def test_query_cache_expires_after_ttl(clock):
    sid = _sid()
    sessions.cache_query(sid, "k", {"found": True})
    assert sessions.cached_query(sid, "k") == {"found": True}
    clock["now"] += sessions._QUERY_TTL_SECONDS + 1
    assert sessions.cached_query(sid, "k") is None


def test_query_cache_evicts_oldest_beyond_cap(clock):
    sid = _sid()
    for i in range(sessions._MAX_QUERY_CACHE + 1):
        sessions.cache_query(sid, f"k{i}", {"i": i})
    assert sessions.cached_query(sid, "k0") is None
    assert sessions.cached_query(sid, f"k{sessions._MAX_QUERY_CACHE}") == {"i": sessions._MAX_QUERY_CACHE}


def test_catalog_cache_expires_after_ttl(clock):
    sid = _sid()
    sessions.cache_catalog(sid, [{"id": "1"}])
    assert sessions.cached_catalog(sid) == [{"id": "1"}]
    clock["now"] += sessions._CATALOG_TTL_SECONDS - 1
    assert sessions.cached_catalog(sid) == [{"id": "1"}]
    clock["now"] += 2
    assert sessions.cached_catalog(sid) is None


def test_orders_cache_expires_after_ttl(clock):
    sid = _sid()
    sessions.cache_orders(sid, [{"order_id": "ORD-1"}])
    assert sessions.cached_orders(sid) == [{"order_id": "ORD-1"}]
    clock["now"] += sessions._ORDERS_TTL_SECONDS + 1
    assert sessions.cached_orders(sid) is None


def test_empty_cached_catalog_is_distinct_from_missing(clock):
    """كتالوج حقيقي فارغ ([]) ≠ لسا ما انحمّل (None) — نفس المبدأ القديم."""
    sid = _sid()
    sessions.cache_catalog(sid, [])
    assert sessions.cached_catalog(sid) == []
