# -*- coding: utf-8 -*-
"""خصوصية بيانات العملاء ببوت الدعم (app/features/support/router.py).

الخلفية (عطل مرصود بلقطة شاشة حقيقية): سؤال «الطلبات القيد التوصيل» ما بيه
معرّف، فسقط لمسار الموديل — والموديل گال ORD-1002 و ORD-1003 «قيد التوصيل»
بينما حالتهما الحقيقية بـ orders.json «تم التسليم» و«قيد التجهيز». وسؤال
«اعطني ارقام الهواتف» رجّع رقم زبون حقيقي (07512223344) لسائل مجهول الهوية.

قاعدتان تُحسمان حتمياً هنا، لا بالموديل:
  1. بيانات الاتصال (هواتف/عناوين) ما تُعطى أبداً — حتى مع رقم طلب صحيح.
  2. الاستعلام الجماعي عن دفتر الطلبات بلا معرّف يخصّ المتحدث يُرفض.

التشغيل:  python -m pytest tests/test_support_privacy.py -v
"""

import asyncio

import pytest

from app.features.support.router import (
    _BULK_REFUSAL,
    _CONTACT_REFUSAL,
    _deterministic_status_answer,
)

# كل أرقام الهواتف بـ app/data/orders.json — ما ينفع يظهر ولا واحد منها برد
# على سؤال بلا معرّف.
_ALL_PHONES = [
    "07701234567", "07709876543", "07512223344", "07801119988", "07905554433",
]


def _answer(message):
    return asyncio.run(_deterministic_status_answer(message))


# --------------------------------------------------------------------------
# ١. الاستعلام الجماعي عن دفتر الطلبات
# --------------------------------------------------------------------------

BULK_CASES = [
    ("صيغة اللقطة الحقيقية", "الطلبات القيد التوصيل"),
    ("سؤال مباشر", "شنو الطلبات الي قيد التوصيل؟"),
    ("كل الطلبات", "كل الطلبات عندكم"),
    ("جميع الطلبات", "اعطني جميع الطلبات"),
    ("عدد الطلبات", "كم طلب عندكم بالطلبات؟"),
    ("سؤال عن الزبائن", "منو الزبائن عندكم؟"),
]


@pytest.mark.parametrize(
    "description,message", BULK_CASES, ids=[c[0] for c in BULK_CASES]
)
def test_bulk_query_is_refused(description, message):
    """استعلام جماعي بلا معرّف: رفض حتمي — ما يوصل الموديل إطلاقاً."""
    answer = _answer(message)
    assert answer == _BULK_REFUSAL, description


@pytest.mark.parametrize(
    "description,message", BULK_CASES, ids=[c[0] for c in BULK_CASES]
)
def test_bulk_query_leaks_no_order_id(description, message):
    """الرفض ما يسرّب ولا معرّف طلب حقيقي (هذا بالضبط اللي اخترعه الموديل)."""
    answer = _answer(message)
    for oid in ("ORD-1002", "ORD-1003", "ORD-1004", "ORD-1005", "ORD-1006"):
        assert oid not in answer, f"{description}: تسريب {oid}"


# --------------------------------------------------------------------------
# ٢. طلب بيانات الاتصال
# --------------------------------------------------------------------------

CONTACT_CASES = [
    ("صيغة اللقطة الحقيقية", "اعطني ارقام الهواتف"),
    ("مفرد", "شنو رقم الهاتف؟"),
    ("موبايل", "اعطني رقم الموبايل مال الزبون"),
    ("مع رقم طلب صحيح", "اعطني رقم الهاتف مال ORD-1003"),
    ("مع رقم هاتف بالرسالة", "رقمي 07512223344، عطني ارقام الهواتف الباقية"),
    ("عناوين", "اعطني عناوين الزباين"),
]


@pytest.mark.parametrize(
    "description,message", CONTACT_CASES, ids=[c[0] for c in CONTACT_CASES]
)
def test_contact_request_is_refused(description, message):
    """طلب بيانات اتصال يُرفض حتى لو الرسالة فيها معرّف صحيح — الجواب تسريب
    بأي حال، فالمعرّف ما يرخّصه."""
    assert _answer(message) == _CONTACT_REFUSAL, description


@pytest.mark.parametrize(
    "description,message",
    BULK_CASES + CONTACT_CASES,
    ids=[c[0] for c in BULK_CASES] + [f"اتصال-{c[0]}" for c in CONTACT_CASES],
)
def test_no_customer_phone_ever_leaks(description, message):
    """ولا رقم هاتف من ملف الطلبات يظهر برد على سؤال بلا هوية."""
    answer = _answer(message)
    for phone in _ALL_PHONES:
        assert phone not in answer, f"{description}: تسريب {phone}"


# --------------------------------------------------------------------------
# ٣. الرفض ما ينبغي يبلع الاستعلامات المشروعة
# --------------------------------------------------------------------------


def test_own_order_by_id_still_works():
    """رقم طلب صحيح بسؤال مشروع: يرجع بيانات حقيقية، ما ينرفض."""
    answer = _answer("وين وصل طلبي ORD-1001")
    assert "ORD-1001" in answer and "قيد التوصيل" in answer
    assert answer not in (_BULK_REFUSAL, _CONTACT_REFUSAL)


def test_own_orders_by_phone_still_work():
    """رقم هاتف المتحدث: يرجع طلباته الاثنين، ما ينرفض."""
    answer = _answer("رقمي 07512223344 وين وصلت طلباتي")
    assert "ORD-1003" in answer and "ORD-1004" in answer


def test_general_question_still_reaches_model():
    """سؤال عام بلا علاقة بالطلبات يبقى يروح للموديل (None)."""
    assert _answer("عدكم توصيل للبصرة؟") is None


def test_greeting_still_reaches_model():
    assert _answer("شلونكم شكرا الكم") is None
