# -*- coding: utf-8 -*-
"""فحص بوابة اكتمال الطلب وذاكرة موقع الجلسة (app/features/sales/router.py).

الخلل اللي تحميه هذي الفحوص، كما ظهر بالإنتاج:
  1. الوكيل ثبّت طلباً بعد الاسم وحده — بلا هاتف ولا عنوان — لأن قاعدة
     التسلسل كانت بالـ system prompt فقط، أي رجاءً للموديل لا قيداً عليه.
  2. الوكيل اخترع عنواناً ("بغداد، الحارثية") بملخّصه، ولأن الاستخراج كان
     يقرأ المحادثة كاملة (سطور الوكيل معها) صارت الهلوسة بيانات طلب.
  3. الحي اللي ذكره العميل مبكراً طلع من نافذة sessions._MAX_TURNS فضاع.

التشغيل:  python -m pytest tests/test_order_gate.py -v
"""

import pytest

from app import sessions
from app.features.sales.router import (
    _fallback_extraction,
    _missing_order_fields,
    _remember_user_location,
)

_PRODUCT = [{"id": "m1", "name": "ماوس لاسلكي لوجيتك", "price": 15000}]


@pytest.fixture
def session_key(request):
    """مفتاح جلسة معزول لكل فحص — الجلسات وحدات عامة بالذاكرة."""
    key = f"sales:test:{request.node.name}"
    sessions._sessions.pop(key, None)
    sessions._session_products.pop(key, None)
    sessions._session_location.pop(key, None)
    return key


def test_missing_phone_and_location_blocks_order(session_key):
    """سيناريو الصورة حرفياً: منتج + اسم + «نعم» بلا هاتف ولا عنوان."""
    sessions.remember_products(session_key, _PRODUCT)
    history = [
        {"role": "user", "content": "اريد ماوس"},
        {"role": "assistant", "content": "أكيد، ماوس لاسلكي لوجيتك بـ15,000 دينار."},
        {"role": "user", "content": "احجز الي"},
        {"role": "assistant", "content": "ماشي، أثبتلك الطلب. اسمك؟"},
        {"role": "user", "content": "امير وسام عبد الستار"},
        {"role": "assistant", "content": "تم تثبيت طلبك، صح هكذا؟"},
    ]
    assert _missing_order_fields(history, "نعم", session_key) == ["phone", "location"]


def test_agent_hallucinated_address_is_not_evidence(session_key):
    """عنوان اخترعه الوكيل بردّه ما يجوز يعدّ عنواناً أعطاه العميل."""
    sessions.remember_products(session_key, _PRODUCT)
    history = [
        {"role": "user", "content": "احجز الي"},
        {"role": "assistant", "content": "تم تثبيت طلبك على عنوان بغداد، الحارثية"},
        {"role": "user", "content": "امير وسام عبد الستار"},
    ]
    for msg in ("احجز الي", "امير وسام عبد الستار"):
        _remember_user_location(session_key, msg)
    missing = _missing_order_fields(history, "نعم", session_key)
    assert "location" in missing
    assert "phone" in missing


def test_complete_order_passes_gate(session_key):
    """طلب مكتمل الأركان ما لازم توقفه البوابة — تدخّلها هنا يقطع بيعاً صحيحاً."""
    sessions.remember_products(session_key, _PRODUCT)
    _remember_user_location(session_key, "اني من بغداد الحارثية")
    history = [
        {"role": "user", "content": "اني من بغداد الحارثية"},
        {"role": "assistant", "content": "هلا بيك"},
        {"role": "user", "content": "امير وسام عبد الستار"},
        {"role": "assistant", "content": "زين"},
        {"role": "user", "content": "07712345678"},
        {"role": "assistant", "content": "تم"},
    ]
    assert _missing_order_fields(history, "نعم ثبت", session_key) == []


def test_confirmation_words_are_not_a_name(session_key):
    """«اي اكيد زين» رسالة عربية بكلمتين — لكنها تأكيد لا اسم عميل."""
    sessions.remember_products(session_key, _PRODUCT)
    history = [{"role": "user", "content": "اريد ماوس هذا"}]
    assert "name" in _missing_order_fields(history, "اي اكيد زين", session_key)


def test_latin_name_accepted(session_key):
    """قسم من الزبائن يكتب اسمه بحرف لاتيني — ما يجوز نسأله مرتين."""
    sessions.remember_products(session_key, _PRODUCT)
    history = [{"role": "user", "content": "اريد ماوس"}]
    assert "name" not in _missing_order_fields(history, "Ameer Wisam", session_key)


def test_single_name_after_agent_asks(session_key):
    """جواب بكلمة وحدة («امير») يُقبل اسماً إذا سبقه سؤال الوكيل عن الاسم."""
    sessions.remember_products(session_key, _PRODUCT)
    history = [
        {"role": "user", "content": "احجز الي"},
        {"role": "assistant", "content": "ماشي، أثبتلك الطلب. اسمك؟"},
    ]
    assert "name" not in _missing_order_fields(history, "امير", session_key)


def test_single_word_without_name_question_is_not_a_name(session_key):
    """نفس الكلمة الوحدة بلا سؤال سابق ما تنعد اسماً — تجنّباً للإيجابي الكاذب."""
    sessions.remember_products(session_key, _PRODUCT)
    history = [
        {"role": "user", "content": "احجز الي"},
        {"role": "assistant", "content": "تدلل، شنو تحب أحجزلك؟"},
    ]
    assert "name" in _missing_order_fields(history, "امير", session_key)


@pytest.mark.parametrize(
    "message",
    ["اني من بغداد الحارثية", "عنواني الكرادة", "اني من الحارثية", "بغداد الكرادة"],
)
def test_address_sentence_is_not_a_name(session_key, message):
    """جملة عنوان تعدّي فحص الحروف والطول لكنها مو اسم — كانت تنحفظ
    كـ customer_name بالطلب («الاسم: اني من بغداد الحارثية»)."""
    sessions.remember_products(session_key, _PRODUCT)
    history = [{"role": "user", "content": "اريد ماوس"}]
    assert "name" in _missing_order_fields(history, message, session_key)


def test_name_extracted_matches_gate(session_key):
    """الاسم اللي تعتمده البوابة هو نفسه اللي يدخل الطلب — كانا منفصلين
    فمرّ طلب بلا اسم رغم أن العميل عرّف بنفسه، فحجبه حارس الإرسال."""
    sessions.remember_products(session_key, _PRODUCT)
    history = [
        {"role": "user", "content": "اريد ماوس"},
        {"role": "assistant", "content": "أكيد"},
        {"role": "user", "content": "اني من بغداد الحارثية"},
        {"role": "assistant", "content": "زين"},
        {"role": "user", "content": "امير وسام عبد الستار"},
        {"role": "assistant", "content": "تم"},
        {"role": "user", "content": "07712345678"},
    ]
    assert _fallback_extraction(history, session_key).customer_name == "امير وسام عبد الستار"


def test_fallback_extraction_uses_session_not_last_message(session_key):
    """عند فشل استخراج الموديل، الطلب البدائي يُبنى من الجلسة لا من «نعم».

    السلوك القديم كان يجعل اسم المنتج «نعم» بلا اسم ولا هاتف ولا عنوان."""
    sessions.remember_products(session_key, _PRODUCT)
    sessions.remember_location(session_key, city="بغداد", district="الحارثية")
    history = [
        {"role": "user", "content": "اريد ماوس"},
        {"role": "assistant", "content": "أكيد"},
        {"role": "user", "content": "07712345678"},
        {"role": "assistant", "content": "تم"},
        {"role": "user", "content": "نعم"},
    ]
    extraction = _fallback_extraction(history, session_key)
    assert extraction.items[0].product_name == _PRODUCT[0]["name"]
    assert extraction.customer_phone == "07712345678"
    assert extraction.customer_district == "الحارثية"
    assert extraction.customer_city == "بغداد"


def test_missing_product_asked_first(session_key):
    """بلا أي منتج بالجلسة، أول حقل ناقص هو المنتج (ترتيب _FIELD_ORDER)."""
    history = [{"role": "user", "content": "هلو"}]
    assert _missing_order_fields(history, "احجزلي", session_key)[0] == "product"


@pytest.mark.parametrize(
    "message, expected_city, expected_district",
    [
        ("اني من بغداد الحارثية", "بغداد", "الحارثية"),
        ("اني من الحارثية", "بغداد", "الحارثية"),
        ("اني من بغداد", "بغداد", ""),
        ("هلو شلونكم", "", ""),
    ],
)
def test_location_memory(session_key, message, expected_city, expected_district):
    """المحافظة والمنطقة تنحفظان سوية — «بغداد الحارثية» ترجع سطرين من
    search_locations والاكتفاء بأولهما كان يضيّع الحي."""
    _remember_user_location(session_key, message)
    known = sessions.known_location(session_key)
    assert known["city"] == expected_city
    assert known["district"] == expected_district


def test_location_memory_survives_later_turns(session_key):
    """الحي المذكور مرة وحدة بأول المحادثة يبقى بعد رسائل ما بيها موقع —
    هذا سبب ضياعه أصلاً (نافذة sessions._MAX_TURNS)."""
    _remember_user_location(session_key, "اني من الحارثية")
    for later in ("اريد ماوس", "شكد سعره", "07712345678", "نعم ثبت"):
        _remember_user_location(session_key, later)
    known = sessions.known_location(session_key)
    assert known["district"] == "الحارثية"
    assert known["city"] == "بغداد"
