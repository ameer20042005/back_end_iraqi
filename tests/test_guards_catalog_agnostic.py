# -*- coding: utf-8 -*-
"""الدروع مستقلة عن صنف التجارة — تُشتق من الكتالوج لا من قوائم بالكود.

المبدأ (بطلب المستخدم): «الدروع لا تبنيها بقيود الكتالوج» — صاحب المحل حر
يضيف أي منتج، والدرع لازم يفهمه فوراً بلا تعديل سطر بايثون.

المخالفة اللي كانت قائمة: `_SPEC_PREFIXES` و`_NON_PRICE_UNITS` قوائم مكتوبة
بالكود ومربوطة بالإلكترونيات (ssd، رام، جيجا، انج). كتالوج مكيفات كان يجعل
«ضاغط انفرتر بقوة 1.5 حصان» رقماً «مختلَقاً» فيُحجب الرد — إنذار كاذب يقطع
البيع بصنف تجارة كامل.

الحل: `_catalog_spec_context` تستخرج من وصف المنتجات نفسها الكلماتِ اللي تحيط
بأرقام غير سعرية، والقوائم المكتوبة تبقى احتياطاً.

الاختبارات هنا تمر على ثلاثة أصناف تجارة مختلفة تماماً، وتحرس اتجاهين:
مواصفات الصنف تمر، والأسعار المخترعة تبقى محجوبة بكل صنف.

التشغيل:  python -m pytest tests/test_guards_catalog_agnostic.py -v
"""

import pytest

from app.guards import check_numbers, check_product_names

# ثلاثة كتالوجات من أصناف تجارة لا تشترك بأي مواصفة.
_ELECTRONICS = (
    "- لابتوب لينوفو IdeaPad 15 | السعر: 750000 IQD | رام 8 جيجا، تخزين 512 SSD."
)
_AC = (
    "- مكيف سبليت 12000 وحدة | السعر: 900000 IQD | "
    "مكيف بقدرة تبريد 12000 وحده وضاغط انفرتر بقوة 1.5 حصان."
)
_FOOD = (
    "- كيس طحين 50 كيلو | السعر: 45000 IQD | طحين فاخر وزن 50 كيلو بجودة عالية."
)
_CAR_PARTS = (
    "- اطار سيارة 205/55 R16 | السعر: 85000 IQD | "
    "اطار مقاس 205 عرض و55 ارتفاع وقطر 16 انج، ضغط 32 رطل."
)


# --------------------------------------------------------------------------
# ١. مواصفات كل صنف تمر بلا إنذار كاذب
# --------------------------------------------------------------------------

SPEC_CASES = [
    ("إلكترونيات: SSD", _ELECTRONICS, "تخزين SSD 512 ورام 8 جيجا"),
    ("مكيفات: ضاغط", _AC, "ضاغط انفرتر بقوة 1.5 حصان"),
    ("مكيفات: قدرة تبريد", _AC, "قدرة التبريد 12000 وحده"),
    ("مواد غذائية: وزن", _FOOD, "كيس وزن 50 كيلو"),
    ("قطع سيارات: ضغط", _CAR_PARTS, "ضغط الاطار 32 رطل"),
    ("قطع سيارات: قطر", _CAR_PARTS, "قطر 16 انج"),
]


@pytest.mark.parametrize(
    "description,catalog,reply", SPEC_CASES, ids=[c[0] for c in SPEC_CASES]
)
def test_catalog_specs_are_not_flagged_as_prices(description, catalog, reply):
    """مواصفة مذكورة بوصف المنتج ما تُقرأ سعراً مهما كان صنف التجارة."""
    assert check_numbers(reply, catalog) == [], description


# --------------------------------------------------------------------------
# ٢. الأسعار المخترعة تبقى محجوبة بكل صنف
# --------------------------------------------------------------------------

INVENTED_PRICE_CASES = [
    ("إلكترونيات", _ELECTRONICS, "بـ600,000 دينار"),
    ("مكيفات", _AC, "المكيف بـ700000 دينار"),
    ("مواد غذائية", _FOOD, "الكيس بـ99,000 دينار"),
    ("قطع سيارات", _CAR_PARTS, "الاطار بـ120,000 دينار"),
]


@pytest.mark.parametrize(
    "description,catalog,reply",
    INVENTED_PRICE_CASES,
    ids=[c[0] for c in INVENTED_PRICE_CASES],
)
def test_invented_prices_still_blocked(description, catalog, reply):
    assert check_numbers(reply, catalog) != [], description


SMUGGLE_CASES = [
    ("سعر بعد كلمة مواصفة من الكتالوج", _AC, "ضاغط 850000 دينار"),
    ("سعر بعد وزن", _FOOD, "وزن 88,000 دينار"),
    ("سعر بعد قطر", _CAR_PARTS, "قطر 150000 دينار"),
]


@pytest.mark.parametrize(
    "description,catalog,reply", SMUGGLE_CASES, ids=[c[0] for c in SMUGGLE_CASES]
)
def test_catalog_derived_exception_does_not_smuggle(description, catalog, reply):
    """الاشتقاق ما يفتح باب تهريب: سعر مسبوق بكلمة مواصفة يبقى محجوباً.

    العملة تُستثنى من الوحدات المشتقة عمداً — بدونها كان «دينار» يصير
    «وحدة مواصفة» لأنه يجي بعد أرقام بالكتالوج."""
    assert check_numbers(reply, catalog) != [], description


# --------------------------------------------------------------------------
# ٣. درع الأسماء كذلك مستقل عن الصنف
# --------------------------------------------------------------------------


def test_product_name_guard_works_for_any_domain():
    """ماركة مو بالكتالوج تُمسك مهما كان الصنف — بلا قائمة ماركات بالكود."""
    assert check_product_names("عدنا مكيف LG انفرتر", _AC)
    assert check_product_names("عدنا اطار Michelin اصلي", _CAR_PARTS)


def test_product_name_guard_accepts_whatever_is_in_the_catalog():
    """ونفس الماركة تُقبل فور إضافتها للكتالوج."""
    catalog_with_lg = _AC + " - مكيف LG انفرتر | السعر: 950000 IQD | مكيف LG."
    assert check_product_names("عدنا مكيف LG انفرتر", catalog_with_lg) == []
