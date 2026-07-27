# -*- coding: utf-8 -*-
"""إصلاحا خطوة تثبيت الطلب بميزة المبيعات (مرصودان بلقطة شاشة إنتاج).

العطلان:

  ١. «نعم اكد» ما ثبّتت الطلب — رد الوكيل انستبدل بجواب تهرب («أحب أتأكد
     منها») واضطر الزبون يعيد التأكيد ثلاث مرات. السبب: حارس الأرقام قرأ
     سعة التخزين «SSD 512» بملخّص الطلب رقماً مالياً مختلَقاً. الوحدات
     غير المالية (_NON_PRICE_UNITS) تُفحص **بعد** الرقم فقط، فـ«512 جيجا»
     تمر و«تخزين SSD 512» تُحجب — ونفس الجملة تجي بكل ملخّص طلب للابتوب،
     فينكسر التثبيت بالضبط عند آخر خطوة وأهمها.

  ٢. المعرّف الختامي كان UUID خام بـ36 خانة يظهر للزبون
     («طلب مؤكَّد — 316f2f31-8764-4d31-b91d-9910176eb927») بدل صيغة النظام
     ORD-#### اللي يعرف يقراها ويلگاها الدعم لما يستعلم عنها.

التشغيل:  python -m pytest tests/test_sales_confirmation.py -v
"""

import re

import pytest

from app.features.sales.service import _new_order_id
from app.guards import check_numbers

# ملخّص الطلب الحقيقي من اللقطة — الجملة اللي كانت تكسر التثبيت.
_ORDER_SUMMARY = (
    "خوش، هذا هو طلبك: لابتوب لينوفو IdeaPad 15 (رام 8 جيجا، تخزين SSD 512 "
    "وخفيف للاستخدام اليومي) عدد 1 بـ750,000 دينار، باسم امير وسام عبد الستار "
    "ورقم هاتفك 07811109151 والتوصيل لحي موسى الكاظم السماوة. تأكدلي الطلب اسوي؟"
)
_CATALOG = "- لابتوب لينوفو IdeaPad 15 | السعر: 750000 IQD | لابتوب خفيف، رام 8 جيجا، تخزين 512 SSD."


# --------------------------------------------------------------------------
# ١. مواصفات المنتج ما تُقرأ أسعاراً
# --------------------------------------------------------------------------


def test_order_summary_passes_the_number_guard():
    """ملخّص الطلب الحقيقي لازم يعدّي — هذا اللي كان يكسر التثبيت."""
    assert check_numbers(_ORDER_SUMMARY, _CATALOG) == []


SPEC_CASES = [
    ("سعة قبلها الوحدة", "تخزين SSD 512"),
    ("سعة بلا وحدة بعدها", "SSD 512 وخفيف"),
    ("رام", "رام 8 وسريع"),
    ("nvme", "nvme 256 داخلي"),
    ("موديل", "موديل 15 الجديد"),
    ("شاشة", "شاشة 15 نحيفة"),
    ("معالج", "معالج 5 حديث"),
]


@pytest.mark.parametrize(
    "description,reply", SPEC_CASES, ids=[c[0] for c in SPEC_CASES]
)
def test_spec_numbers_are_not_prices(description, reply):
    assert check_numbers(reply, _CATALOG) == [], description


# --------------------------------------------------------------------------
# ٢. الاستثناء ما ينفتح بوابة تهريب أسعار
# --------------------------------------------------------------------------

SMUGGLE_CASES = [
    ("سعر بعد كلمة مواصفة", "عدنا شاشة 900000 دينار"),
    ("سعر بفواصل بعد مواصفة", "تخزين 900,000 دينار"),
    ("سعر كبير بعد رام", "رام 850000 دينار"),
    ("سعر بلا فواصل بعد موديل", "موديل 999000 دينار"),
]


@pytest.mark.parametrize(
    "description,reply", SMUGGLE_CASES, ids=[c[0] for c in SMUGGLE_CASES]
)
def test_spec_exception_does_not_smuggle_prices(description, reply):
    """الاستثناء مقيّد بالأرقام القصيرة غير المتبوعة بعملة — سعر مختلَق
    مسبوق بكلمة مواصفة لازم يبقى محجوباً."""
    assert check_numbers(reply, _CATALOG) != [], description


def test_catalog_price_still_allowed():
    """السعر الحقيقي من الكتالوج يمر عادي."""
    assert check_numbers("سعره 750,000 دينار", _CATALOG) == []


def test_invented_price_still_blocked():
    """سعر مختلَق بلا أي كلمة مواصفة يبقى محجوباً."""
    assert check_numbers("أنطيك ياه بـ600,000 دينار", _CATALOG) != []


# --------------------------------------------------------------------------
# ٣. صيغة معرّف الطلب
# --------------------------------------------------------------------------

_ORDER_ID_RE = re.compile(r"^ORD-[0-9A-F]{6}$")


def test_order_id_uses_system_format():
    """ORD-#### مثل app/data/orders.json — مو UUID خام."""
    assert _ORDER_ID_RE.match(_new_order_id())


def test_order_id_is_short_enough_to_read():
    """الزبون لازم يگدر يقراه ويعيد كتابته للدعم."""
    assert len(_new_order_id()) <= 12


def test_order_ids_are_unique():
    assert len({_new_order_id() for _ in range(500)}) == 500


def test_order_id_is_findable_by_support():
    """معرّف المبيعات لازم يمسكه مستخرِج الدعم — وإلا الزبون يعطي رقم طلبه
    لبوت الدعم فما يلگاه."""
    from app.features.support.router import extract_order_id

    order_id = _new_order_id()
    assert extract_order_id(f"وين وصل طلبي {order_id}؟") is not None
