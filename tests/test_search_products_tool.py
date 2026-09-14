# -*- coding: utf-8 -*-
"""search_products_tool (app/tools/products.py) — تصميم "حمّل الكتالوج مرة
وحدة بالجلسة": أول استدعاء بجلسة يجيب الكتالوج كاملاً من
product_repository.list_all ويخزّنه بكاش الجلسة؛ أي استدعاء لاحق بنفس
الجلسة يلگى الكاش مباشرة بلا أي نداء HTTP جديد.

وبعد docs/fix-plan.md § المرحلة 8: `query` صارت تطابق فعلاً على الكتالوج
الكامل (العطل B2)، والرد يحمل found/count/showing/hint (العطل B1).

التشغيل:  python -m pytest tests/test_search_products_tool.py -v
"""

import asyncio
import uuid

import pytest

from app import sessions
from app.config import settings
from app.tools import products as products_tool


_API_KEY = "test-key"
_CATALOG = [
    {"id": "1", "name": "غسالة اتوماتيك LG", "category": "غسالات", "in_stock": True,
     "description": "سعة 8 كيلو"},
    {"id": "2", "name": "ثلاجة سامسونج", "category": "ثلاجات", "in_stock": False,
     "description": "بابين نوفروست"},
]


class _FakeProductRepository:
    def __init__(self, catalog):
        self.catalog = catalog
        self.list_all_calls = 0

    async def list_all(self, api_key):
        self.list_all_calls += 1
        return list(self.catalog)

    async def search(self, query, api_key, top_k=5, category=None, in_stock_only=False):
        raise AssertionError("search() ما يفترض يُستدعى — البحث محلي على الكاش")

    async def get_by_id(self, product_id, api_key):
        raise NotImplementedError


@pytest.fixture(autouse=True)
def fake_repository(monkeypatch):
    fake = _FakeProductRepository(_CATALOG)
    monkeypatch.setattr(products_tool, "product_repository", fake)
    return fake


def _session_id():
    return f"test-search-{uuid.uuid4()}"


def _call(args, sid):
    return asyncio.run(products_tool.search_products_tool(args, _API_KEY, session_id=sid))


def test_first_call_fetches_and_caches_full_catalog(fake_repository):
    sid = _session_id()
    result = _call({}, sid)
    assert fake_repository.list_all_calls == 1
    assert result["found"] is True
    assert result["count"] == 2
    assert {p["id"] for p in result["results"]} == {"1", "2"}
    assert sessions.cached_catalog(sid) == _CATALOG


def test_second_call_same_session_uses_cache_no_http(fake_repository):
    sid = _session_id()
    _call({}, sid)
    _call({"query": "غسالة"}, sid)
    # نداء list_all وحد بس رغم استدعائين — الثاني لگى الكاش مباشرة.
    assert fake_repository.list_all_calls == 1


def test_query_matches_by_name():
    """إصلاح العطل B2: query تضيّق فعلاً — «غساله» (بلا همزة/بتاء مربوطة
    مختلفة) تطابق «غسالة اتوماتيك LG» بعد التطبيع."""
    result = _call({"query": "غساله"}, _session_id())
    assert [p["id"] for p in result["results"]] == ["1"]
    assert result["count"] == 1


def test_query_matches_by_description():
    """الزبون قد يسمي المنتج بكلمة من وصفه لا باسمه الرسمي."""
    result = _call({"query": "نوفروست"}, _session_id())
    assert [p["id"] for p in result["results"]] == ["2"]


def test_query_without_match_returns_found_false():
    """ماكو مطابقة → found=false صراحةً (لا قائمة كاملة تربك الموديل)، حتى
    يقول «ماكو» بثقة مبنية على بحث فعلي."""
    result = _call({"query": "شي ما موجود إطلاقاً"}, _session_id())
    assert result["found"] is False
    assert result["results"] == []
    assert "message" in result


def test_short_words_do_not_filter():
    """كلمات أقصر من 3 أحرف (حروف جر) تُهمَل — استعلام منها فقط = الكتالوج كله."""
    result = _call({"query": "من لي"}, _session_id())
    assert result["count"] == 2


def test_category_filter_narrows_this_round_only():
    sid = _session_id()
    result = _call({"category": "غسالات"}, sid)
    assert [p["id"] for p in result["results"]] == ["1"]
    # الكاش نفسه يبقى كامل (بلا فلترة) رغم فلترة رد هذا الدور.
    assert len(sessions.cached_catalog(sid)) == 2


def test_in_stock_only_filter():
    result = _call({"in_stock_only": True}, _session_id())
    assert [p["id"] for p in result["results"]] == ["1"]


def test_capped_result_announces_total_with_hint(monkeypatch):
    """القصّ بسقف الحقن يُعلَن للموديل: count الكلي + showing + hint (B1)."""
    monkeypatch.setattr(settings, "max_injected_records", 1)
    result = _call({}, _session_id())
    assert result["count"] == 2
    assert result["showing"] == 1
    assert "من أصل 2" in result["hint"]


def test_empty_catalog_returns_message(monkeypatch):
    monkeypatch.setattr(products_tool, "product_repository", _FakeProductRepository([]))
    result = _call({}, _session_id())
    assert result["found"] is False
    assert result["results"] == []
    assert "message" in result
