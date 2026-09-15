# -*- coding: utf-8 -*-
"""كاش كتالوج الجلسة بـTTL (app/sessions.py).

التشغيل:  python -m pytest tests/test_sessions_ttl.py -v
"""

import uuid

import pytest

from app import sessions
@pytest.fixture
def clock(monkeypatch):
    """ساعة وهمية بدل time.monotonic حتى نقدّم الوقت بلا انتظار فعلي."""
    state = {"now": 1000.0}
    monkeypatch.setattr(sessions.time, "monotonic", lambda: state["now"])
    return state


def _sid():
    return f"test-ttl-{uuid.uuid4()}"


def test_catalog_cache_expires_after_ttl(clock):
    sid = _sid()
    sessions.cache_catalog(sid, [{"id": "1"}])
    assert sessions.cached_catalog(sid) == [{"id": "1"}]
    clock["now"] += sessions._CATALOG_TTL_SECONDS - 1
    assert sessions.cached_catalog(sid) == [{"id": "1"}]
    clock["now"] += 2
    assert sessions.cached_catalog(sid) is None


def test_empty_cached_catalog_is_distinct_from_missing(clock):
    """كتالوج حقيقي فارغ ([]) ≠ لسا ما انحمّل (None) — نفس المبدأ القديم."""
    sid = _sid()
    sessions.cache_catalog(sid, [])
    assert sessions.cached_catalog(sid) == []
