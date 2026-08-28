# -*- coding: utf-8 -*-
"""كاش الكتالوج الكامل على مستوى الجلسة (app/sessions.py::cache_catalog/
cached_catalog) — نفس نمط cache_orders/cached_orders الموجود أصلاً، لكن
للمنتجات بدل الطلبات (انظر next.md: "تحميل الكتالوج مرة وحدة بالجلسة").

التشغيل:  python -m pytest tests/test_sessions_catalog.py -v
"""

import uuid

from app import sessions


def _session_id():
    """معرّف جلسة فريد لكل اختبار — الكاش قاموس بمستوى الوحدة (module-level)
    يبقى بين الاختبارات، فبلا هذا كانت النتائج تتسرّب بين الحالات."""
    return f"test-catalog-{uuid.uuid4()}"


def test_cached_catalog_none_before_first_cache():
    """قبل أي cache_catalog: None، لا [] — يميّز "لسا ما انحمّل" عن "انحمّل
    وطلع فاضي فعلاً" (نفس مبدأ cached_orders)."""
    assert sessions.cached_catalog(_session_id()) is None


def test_cache_catalog_then_cached_catalog_roundtrip():
    sid = _session_id()
    products = [{"id": "1", "name": "غسالة اتوماتيك"}, {"id": "2", "name": "ثلاجة"}]
    sessions.cache_catalog(sid, products)
    assert sessions.cached_catalog(sid) == products


def test_cache_catalog_isolated_per_session():
    sid_a, sid_b = _session_id(), _session_id()
    sessions.cache_catalog(sid_a, [{"id": "1", "name": "منتج أ"}])
    assert sessions.cached_catalog(sid_b) is None


def test_cache_catalog_overwrites_previous_value():
    """استدعاء ثانٍ بنفس الجلسة (نظرياً ما يصير من search_products_tool —
    يفحص الكاش أولاً — لكن الدالة نفسها لازم تسمح بالاستبدال لو احتاج
    مستقبلاً إعادة تحميل صريحة)."""
    sid = _session_id()
    sessions.cache_catalog(sid, [{"id": "1", "name": "قديم"}])
    sessions.cache_catalog(sid, [{"id": "2", "name": "جديد"}])
    assert sessions.cached_catalog(sid) == [{"id": "2", "name": "جديد"}]
