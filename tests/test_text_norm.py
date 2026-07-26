# -*- coding: utf-8 -*-
"""فحص الطبقة ١ — التطبيع (app/text_norm.py).

التشغيل:  python -m pytest tests/test_text_norm.py -v
"""

import pytest

from app.text_norm import normalize, reduce_repeats

CASES = [
    ("توحيد الألف بأشكالها", "أحمد إياد آمنة ٱسم", "احمد اياد امنه اسم"),
    ("الألف المقصورة → ياء", "مصطفى على", "مصطفي علي"),
    ("التاء المربوطة → هاء", "سماعة بلوتوث", "سماعه بلوتوث"),
    ("حذف التشكيل", "مَرْحَبًا بِيكُم", "مرحبا بيكم"),
    ("حذف التطويل", "مرحـــــبا", "مرحبا"),
    ("أرقام عربية-هندية", "السعر ٧٥٠٠٠٠", "السعر 750000"),
    ("أرقام فارسية", "السعر ۷۵۰۰۰۰", "السعر 750000"),
    ("تقليص الحروف المكررة", "هلللللو شلوووونك", "هللو شلوونك"),
    ("حرفان متتاليان يبقيان", "الله ربي", "الله ربي"),
    ("تجريد الترقيم العربي", "شلونك، زين؟ خوش!", "شلونك زين خوش"),
    ("الترقيم يفصل الكلمات", "سعر،اللابتوب", "سعر اللابتوب"),
    ("تقليص الفراغات", "  شلون    الحال  ", "شلون الحال"),
    ("الهمزة على الواو والياء", "مسؤول ورئيس", "مسوول ورييس"),
    ("الكاف والياء الفارسية", "کیف", "كيف"),
    ("نص فارغ", "", ""),
]


@pytest.mark.parametrize(
    "description,raw,expected", CASES, ids=[c[0] for c in CASES]
)
def test_normalize(description, raw, expected):
    assert normalize(raw) == expected, description


def test_repeats_never_touch_digits():
    """أرقام الهواتف العراقية فيها خانات مكررة (٠٧٧٧...) — تقليصها يخرّب الرقم."""
    assert reduce_repeats("07771111222") == "07771111222"
    assert normalize("رقمي ٠٧٧٧١١١٢٢٢٢") == "رقمي 07771112222"


def test_normalize_is_idempotent():
    """تطبيع النص المطبَّع لا يغيّره — شرط لازم حتى يتطابق الفهرس مع الاستعلام
    مهما تكرر تمرير النص بالطبقة."""
    for _, raw, _ in CASES:
        once = normalize(raw)
        assert normalize(once) == once


PREFIX_CASES = [
    ("واو العطف + ال", "زين واللابتوب؟", "لابتوب"),
    ("باء الجر + ال", "شكد بالسماعة", "سماعه"),
    ("لام الجر (لل)", "سعر للطاولة", "طاوله"),
    ("فاء + ال", "فالحقيبة بيش", "حقيبه"),
    ("كاف + ال", "شي كالماوس", "ماوس"),
    ("ال وحدها", "الماوس بيش", "ماوس"),
    ("بدون بادئة", "لابتوب لينوفو", "لابتوب"),
]


@pytest.mark.parametrize(
    "description,query,expected_token",
    PREFIX_CASES,
    ids=[c[0] for c in PREFIX_CASES],
)
def test_definite_article_prefixes_are_stripped(description, query, expected_token):
    """«واللابتوب» لازم توصل لجذر «لابتوب» وإلا البحث يرجع صفر نتائج فيرد
    الوكيل رد التهرب («أتأكدلك») على سؤال سعر مشروع — مرصود بمحادثة فعلية."""
    from app.rag.retriever import tokenize

    assert expected_token in tokenize(query), description


def test_prefixed_query_retrieves_the_product():
    """الفحص من طرف لطرف: السؤال ببادئة لازم يرجّع المنتج فعلاً."""
    from app.products import product_repository

    hits = product_repository.search("زين واللابتوب؟", top_k=3)
    assert any("لابتوب" in h["name"] for h in hits)


def test_index_and_query_share_one_normalizer():
    """`app.rag.retriever.normalize` لازم تكون هي نفسها دالة الطبقة ١ حرفياً —
    أي نسخة محلية بالمسترجع تكسر البحث بصمت."""
    from app.rag.retriever import normalize as retriever_normalize

    assert retriever_normalize is normalize
