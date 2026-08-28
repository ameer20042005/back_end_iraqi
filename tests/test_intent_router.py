# -*- coding: utf-8 -*-
"""راوتر النية المحافظ (app/intent_router.py) — next.md §2 "التوصية النهائية".

الخلفية: وكيلا المبيعات والدعم كانا يستدعيان أداة (search_products/
get_order_status) حتى لرسائل تحية/شكر/هوية بحتة. الفحص هنا محافظ بالاتجاهين
(انظر تعليق الملف): يصنّف "دردشة بحتة" فقط لو ماكو رقم ولا كلمة إشارة بيانات
معها — رسالة مختلطة تبقى تستدعي الأداة كالمعتاد.

التشغيل:  python -m pytest tests/test_intent_router.py -v
"""

from app.intent_router import is_pure_chitchat


# ---------------------------------------------------------------------------
# دردشة بحتة — True
# ---------------------------------------------------------------------------

def test_greeting_alone_is_chitchat():
    assert is_pure_chitchat("هلا") is True
    assert is_pure_chitchat("السلام عليكم") is True
    assert is_pure_chitchat("مرحبا شلونك") is True


def test_thanks_alone_is_chitchat():
    assert is_pure_chitchat("شكراً الك") is True
    assert is_pure_chitchat("تسلم حبيبي") is True


def test_farewell_alone_is_chitchat():
    assert is_pure_chitchat("باي مع السلامة") is True


def test_identity_question_is_chitchat():
    assert is_pure_chitchat("شنو اسمك؟") is True
    assert is_pure_chitchat("منو انت؟") is True
    assert is_pure_chitchat("انت بوت لو انسان؟") is True


# ---------------------------------------------------------------------------
# رسالة مختلطة (تحية + إشارة بيانات) — False، لازم تستدعي الأداة
# ---------------------------------------------------------------------------

def test_greeting_with_product_signal_is_not_chitchat():
    """next.md §2: "هلا، شنو عدكم غسالات؟" لازم يستدعي search_products."""
    assert is_pure_chitchat("هلا، شنو عدكم غسالات؟") is False


def test_greeting_with_price_word_is_not_chitchat():
    assert is_pure_chitchat("هلا شكد سعر اللابتوب؟") is False


def test_thanks_with_order_tracking_is_not_chitchat():
    assert is_pure_chitchat("شكراً، بس وين وصل طلبي؟") is False


# ---------------------------------------------------------------------------
# رسالة فيها رقم (هاتف/طلب) — False حتى لو صادف كلمة تحية
# ---------------------------------------------------------------------------

def test_message_with_phone_number_is_not_chitchat():
    assert is_pure_chitchat("هلا، رقمي 07711234567") is False


def test_message_with_order_id_is_not_chitchat():
    assert is_pure_chitchat("مرحبا ORD-1042 وين وصل؟") is False


# ---------------------------------------------------------------------------
# رسائل عادية غير متعلقة — False (لا تطابق أي نمط دردشة أصلاً)
# ---------------------------------------------------------------------------

def test_unrelated_message_is_not_chitchat():
    assert is_pure_chitchat("اريد اغير عنوان التوصيل") is False


def test_empty_message_is_not_chitchat():
    assert is_pure_chitchat("") is False
    assert is_pure_chitchat("   ") is False
