# -*- coding: utf-8 -*-
"""search_products_tool (app/tools/products.py) — تصميم "حمّل الكتالوج مرة
وحدة بالجلسة" (next.md): أول استدعاء بجلسة يجيب الكتالوج كاملاً من
product_repository.list_all ويخزّنه بكاش الجلسة؛ أي استدعاء لاحق بنفس
الجلسة يلگى الكاش مباشرة بلا أي نداء HTTP جديد.

التشغيل:  python -m pytest tests/test_search_products_tool.py -v
"""

import asyncio
import uuid

import pytest

from app import sessions
from app.tools import products as products_tool


_API_KEY = "test-key"
_CATALOG = [
    {"id": "1", "name": "غسالة اتوماتيك LG", "category": "غسالات", "in_stock": True},
    {"id": "2", "name": "ثلاجة سامسونج", "category": "ثلاجات", "in_stock": False},
]


class _FakeProductRepository:
    def __init__(self, catalog):
        self.catalog = catalog
        self.list_all_calls = 0

    async def list_all(self, api_key):
        self.list_all_calls += 1
        return list(self.catalog)

    async def search(self, query, api_key, top_k=5, category=None, in_stock_only=False):
        raise AssertionError("search() ما يفترض يُستدعى بعد التصميم الجديد")

    async def get_by_id(self, product_id, api_key):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def fake_repository(monkeypatch):
    fake = _FakeProductRepository(_CATALOG)
    monkeypatch.setattr(products_tool, "product_repository", fake)
    return fake


def _session_id():
    return f"test-search-{uuid.uuid4()}"


def test_first_call_fetches_and_caches_full_catalog(fake_repository):
    sid = _session_id()
    result = asyncio.run(products_tool.search_products_tool({}, _API_KEY, session_id=sid))
    assert fake_repository.list_all_calls == 1
    assert {p["id"] for p in result["results"]} == {"1", "2"}
    assert sessions.cached_catalog(sid) == _CATALOG


def test_second_call_same_session_uses_cache_no_http(fake_repository):
    sid = _session_id()
    asyncio.run(products_tool.search_products_tool({}, _API_KEY, session_id=sid))
    asyncio.run(products_tool.search_products_tool({"query": "غسالة"}, _API_KEY, session_id=sid))
    # نداء list_all وحد بس رغم استدعائين — الثاني لگى الكاش مباشرة.
    assert fake_repository.list_all_calls == 1


def test_query_arg_is_ignored_and_does_not_filter(fake_repository):
    """query تُقرأ وتُهمَل عمداً (قرار next.md: الموديل يفلتر من الكتالوج
    المحقون، لا الأداة) — استعلام لا يطابق أي منتج لازم يرجّع الكتالوج
    كاملاً برضو، لا نتيجة فاضية."""
    sid = _session_id()
    result = asyncio.run(
        products_tool.search_products_tool({"query": "شي ما موجود إطلاقاً"}, _API_KEY, session_id=sid)
    )
    assert {p["id"] for p in result["results"]} == {"1", "2"}


def test_category_filter_narrows_this_round_only(fake_repository):
    sid = _session_id()
    result = asyncio.run(
        products_tool.search_products_tool({"category": "غسالات"}, _API_KEY, session_id=sid)
    )
    assert [p["id"] for p in result["results"]] == ["1"]
    # الكاش نفسه يبقى كامل (بلا فلترة) رغم فلترة رد هذا الدور.
    assert len(sessions.cached_catalog(sid)) == 2


def test_in_stock_only_filter(fake_repository):
    sid = _session_id()
    result = asyncio.run(
        products_tool.search_products_tool({"in_stock_only": True}, _API_KEY, session_id=sid)
    )
    assert [p["id"] for p in result["results"]] == ["1"]


def test_empty_catalog_returns_message(fake_repository, monkeypatch):
    monkeypatch.setattr(products_tool, "product_repository", _FakeProductRepository([]))
    sid = _session_id()
    result = asyncio.run(products_tool.search_products_tool({}, _API_KEY, session_id=sid))
    assert result["results"] == []
    assert "message" in result
