# -*- coding: utf-8 -*-
"""حرّاس الأرقام/الأسماء (app/guards.py) مع الكتالوج المحقون بلا استدعاء
أداة بنفس الدور (app/features/sales/router.py::_tool_reference_text /
_apply_number_and_name_guards).

الخلفية (اكتشاف أثناء تصميم next.md — "تحميل الكتالوج مرة وحدة بالجلسة"):
الحرّاس كانت تبني مرجعها من tool_calls بنفس الدور فقط. بعد الانتقال لحقن
الكتالوج الكامل بالبرومبت (بدل استدعاء أداة كل دور)، أغلب الأدوار ما فيها
tool_calls إطلاقاً — فلو ما ضُمّ الكتالوج المحقون لمرجع الحرّاس، كل رد بعد
الدور الأول كان ينحجب (يُعتبر مختلَقاً) رغم صحته الكاملة من الكتالوج.

التشغيل:  python -m pytest tests/test_sales_catalog_guards.py -v
"""

from app.features.sales.router import _apply_number_and_name_guards, _tool_reference_text

_CATALOG = [
    {"id": "1", "name": "غسالة اتوماتيك LG", "price": 500000, "currency": "IQD"},
]


def test_tool_reference_text_empty_when_no_tool_calls_and_no_catalog():
    """السلوك الأصلي يبقى كما هو: بلا tool_calls وبلا كتالوج، المرجع فاضي."""
    assert _tool_reference_text([], catalog=None) == ""


def test_tool_reference_text_includes_catalog_even_without_tool_calls():
    """هذا هو الإصلاح نفسه: كتالوج محقون بلا أي tool_call بنفس الدور لازم
    يظهر بالمرجع."""
    ref = _tool_reference_text([], catalog=_CATALOG)
    assert "غسالة اتوماتيك LG" in ref
    assert "500000" in ref


def test_reply_from_injected_catalog_is_not_blocked():
    """رد يذكر اسم وسعر المنتج **من الكتالوج المحقون فقط** (tool_calls فاضية
    هذا الدور) — ما يفترض يُستبدل بـ_SAFE_REDIRECT."""
    answer = "خوش، عدنا غسالة اتوماتيك LG بسعر 500,000 دينار ومتوفرة بالمخزون."
    result_answer, blocked = _apply_number_and_name_guards(
        answer, tool_calls=[], session_id="s1", catalog=_CATALOG
    )
    assert blocked is False
    assert result_answer == answer  # ما تغيّر شي — الاسم والسعر كلاهما مسندان


def test_reply_with_name_not_in_catalog_still_blocked():
    """الحارس يبقى يمسك اسماً مختلَقاً حتى مع كتالوج محقون — الكتالوج
    توسيع للمرجع لا إلغاء للفحص."""
    answer = "عدنا لابتوب Asus Core i7 رام 16 بسعر 900,000 دينار."
    _result_answer, blocked = _apply_number_and_name_guards(
        answer, tool_calls=[], session_id="s1", catalog=_CATALOG
    )
    assert blocked is True


def test_tool_reference_text_merges_tool_calls_and_catalog():
    """دور فيه استدعاء أداة فعلي **و** كتالوج محقون سابقاً (نادر لكن ممكن —
    مثلاً منتج جديد بعد ما انحمّل الكتالوج) — كلاهما يوصل المرجع."""
    tool_calls = [{
        "tool": "search_products",
        "args": {},
        "result": {"results": [{"id": "2", "name": "ثلاجة سامسونج", "price": 700000}]},
    }]
    ref = _tool_reference_text(tool_calls, catalog=_CATALOG)
    assert "غسالة اتوماتيك LG" in ref
    assert "ثلاجة سامسونج" in ref
