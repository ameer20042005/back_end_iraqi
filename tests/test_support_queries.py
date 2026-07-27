# -*- coding: utf-8 -*-
"""استعلامات الموظفين التشغيلية ببوت الدعم (app/features/support/router.py).

الخلفية (عطل مرصود بلقطة شاشة حقيقية): سؤال «الطلبات القيد التوصيل» رجّع
ORD-1002 و ORD-1003 بينما حالتهما بـ orders.json «تم التسليم» و«قيد التجهيز».
الطلبات الصحيحة قيد التوصيل هي ORD-1001 و ORD-1005.

السبب الجذري: أداة `get_order_status` ما كانت تقبل إلا order_id/phone، فأي
استعلام بالحالة يرجع خطأ مهما صيغته — والموديل، وهو مأمور «لا تجاوب من عندك»
وبلا مخرج، ما بقى أمامه إلا الاختراع.

البوت **داخلي للموظفين**، فهذي الاستعلامات مشروعة: الإصلاح إننا نجاوبها
حتمياً من المصدر، مو نرفضها. كل معرّف وحالة ورقم بالرد لازم يكون من
orders.json حرفياً.

التشغيل:  python -m pytest tests/test_support_privacy.py -v
"""

import asyncio

import pytest

from app.features.support.router import (
    _deterministic_status_answer,
    extract_status,
)

# الحقيقة الأرضية من app/data/orders.json — أي انحراف عنها هلوسة.
_BY_STATUS = {
    "قيد التوصيل": ["ORD-1001", "ORD-1005"],
    "قيد التجهيز": ["ORD-1003"],
    "تم التسليم": ["ORD-1002", "ORD-1004"],
    "ملغي": ["ORD-1006"],
}
_ALL_IDS = ["ORD-1001", "ORD-1002", "ORD-1003", "ORD-1004", "ORD-1005", "ORD-1006"]


def _answer(message):
    return asyncio.run(_deterministic_status_answer(message))


# --------------------------------------------------------------------------
# ١. استخراج الحالة من صيغ الموظف
# --------------------------------------------------------------------------

STATUS_CASES = [
    ("صيغة اللقطة الحقيقية", "الطلبات القيد التوصيل", "قيد التوصيل"),
    ("سؤال مباشر", "شنو الطلبات الي قيد التوصيل؟", "قيد التوصيل"),
    ("اختصار", "شوفلي التوصيل", "قيد التوصيل"),
    ("مرادف عامي", "الطلبات اللي بالطريق", "قيد التوصيل"),
    ("تجهيز", "شنو الطلبات قيد التجهيز", "قيد التجهيز"),
    ("تسليم", "الطلبات تم التسليم", "تم التسليم"),
    ("مسلمة عامي", "الطلبات المسلمه", "تم التسليم"),
    ("مكتملة — لقطة الإنتاج", "مكتمله", "تم التسليم"),
    ("مكتملة بالتاء المربوطة", "الطلبات المكتملة", "تم التسليم"),
    ("منجزة", "شنو الطلبات المنجزه", "تم التسليم"),
    ("تم توصيلها", "الطلبات التي تم توصيلها", "تم التسليم"),
    ("ملغي", "شنو الطلبات الملغيه", "ملغي"),
    ("مرتجعة", "الطلبات المرتجعه", "ملغي"),
    ("بالمخزن", "الطلبات بالمخزن", "قيد التجهيز"),
    ("بلا حالة", "شلونكم اليوم", None),
]


@pytest.mark.parametrize(
    "description,message,expected",
    STATUS_CASES,
    ids=[c[0] for c in STATUS_CASES],
)
def test_extract_status(description, message, expected):
    assert asyncio.run(extract_status(message)) == expected, description


def test_longest_alias_wins():
    """«قيد التوصيل» تحتوي «التوصيل» — لازم نطابق الأطول لا الأقصر."""
    assert asyncio.run(extract_status("الطلبات قيد التوصيل")) == "قيد التوصيل"


def test_statuses_are_derived_from_data_not_hardcoded():
    """الحالات تُقرأ من orders.json وقت التشغيل — حالة جديدة بالبيانات تشتغل
    فوراً بلا تعديل كود (نفس مبدأ دروع المبيعات)."""
    from app.features.support.router import _known_statuses

    statuses = asyncio.run(_known_statuses())
    assert set(statuses) == set(_BY_STATUS)


# --------------------------------------------------------------------------
# ٢. الجواب مبني من المصدر — لا اختراع ولا إغفال
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected_ids",
    list(_BY_STATUS.items()),
    ids=list(_BY_STATUS),
)
def test_status_query_lists_exactly_the_right_orders(status, expected_ids):
    """كل طلب بهذي الحالة يُذكر، وولا طلب بغيرها يتسلل للرد."""
    answer = _answer(f"شنو الطلبات {status}؟")
    assert answer is not None, "السؤال راح للموديل بدل ما ينحسم حتمياً"
    for oid in expected_ids:
        assert oid in answer, f"{status}: الطلب {oid} ناقص من الرد"
    for oid in set(_ALL_IDS) - set(expected_ids):
        assert oid not in answer, f"{status}: الطلب {oid} ما لازم يظهر"


def test_the_exact_screenshot_bug_is_fixed():
    """العطل الأصلي حرفياً: «الطلبات القيد التوصيل» كان يرجّع ORD-1002
    و ORD-1003 (حالتهما تم التسليم/قيد التجهيز)."""
    answer = _answer("الطلبات القيد التوصيل")
    assert "ORD-1001" in answer and "ORD-1005" in answer
    assert "ORD-1002" not in answer, "رجعت هلوسة اللقطة الأصلية"
    assert "ORD-1003" not in answer, "رجعت هلوسة اللقطة الأصلية"


def test_list_all_covers_every_order():
    answer = _answer("اعطني كل الطلبات")
    assert answer is not None
    for oid in _ALL_IDS:
        assert oid in answer


def test_phones_are_shown_to_staff():
    """البوت داخلي: أرقام الهواتف بيانات شغل الموظف، تُعرض ما تُحجب."""
    answer = _answer("اعطني ارقام الهواتف")
    assert answer is not None
    for phone in ("07701234567", "07709876543", "07512223344"):
        assert phone in answer


def test_status_query_includes_phone_for_contact():
    """قائمة الحالة تحمل رقم الهاتف حتى يگدر الموظف يتصل بالزبون."""
    answer = _answer("شنو الطلبات قيد التوصيل؟")
    assert "07701234567" in answer and "07801119988" in answer


def test_unknown_status_never_invents_orders():
    """حالة ما موجودة بالبيانات إطلاقاً: ممنوع اختراع قائمة.

    ملاحظة: «المرتجعه» **مو** مثالاً صالحاً هنا — هي مرادف دارج لـ«ملغي»
    وترجع ORD-1006 عن حق. نستعمل حالة ما تقابل أي شي بالبيانات."""
    answer = _answer("شنو الطلبات المعلقه بانتظار الدفع؟")
    if answer is not None:
        for oid in _ALL_IDS:
            assert oid not in answer


# --------------------------------------------------------------------------
# ٣. الاستعلامات القديمة ما انكسرت
# --------------------------------------------------------------------------


def test_specific_order_id_beats_status_listing():
    """«حالة ORD-1001» ترجع ذاك الطلب بالذات، مو قائمة كل قيد التوصيل."""
    answer = _answer("شنو حالة ORD-1001؟")
    assert "ORD-1001" in answer and "ORD-1005" not in answer


def test_own_order_by_id_still_works():
    answer = _answer("وين وصل طلبي ORD-1001")
    assert "ORD-1001" in answer and "قيد التوصيل" in answer


def test_orders_by_phone_still_work():
    answer = _answer("رقمي 07512223344 وين وصلت طلباتي")
    assert "ORD-1003" in answer and "ORD-1004" in answer


def test_general_question_still_reaches_model():
    """سؤال عام بلا علاقة بالطلبات يبقى يروح للموديل (None)."""
    assert _answer("عدكم توصيل للبصرة؟") is None


# --------------------------------------------------------------------------
# ٤. المتابعة، العدّ، والأحدث — حالات مرصودة بلقطة إنتاج
# --------------------------------------------------------------------------


def test_followup_resolves_status_from_history():
    """«اعطني هذه الطلبات» بلا سياق ما تعني شي، ومعه تعني الحالة السابقة.

    باللقطة: الموظف كتب «مكتمله» ثم وضّح ثم گال «اعطني هذه الطلبات» — وكل
    مرة يرد البوت «أبحثلك هسه» بلا ما يبحث شي."""
    history = [
        {"role": "user", "content": "مكتمله"},
        {"role": "assistant", "content": "..."},
        {"role": "user", "content": "المكتمله تعني التي تم توصيلها"},
        {"role": "assistant", "content": "..."},
    ]
    answer = asyncio.run(_deterministic_status_answer("اعطني هذه الطلبات", history))
    assert answer is not None, "المتابعة لسه تروح للموديل"
    assert "ORD-1002" in answer and "ORD-1004" in answer
    assert "ORD-1001" not in answer


def test_followup_without_history_still_reaches_model():
    """بلا سياق ما نخمّن — «اعطني هذه الطلبات» لحالها تروح للموديل."""
    assert _answer("اعطني هذه الطلبات") is None


def test_count_question_answers_with_a_number():
    """«كم طلب مكتمل؟» يريد رقماً، والقائمة وحدها تخلي الموظف يعد بنفسه."""
    answer = _answer("كم طلب مكتمل؟")
    assert answer is not None
    assert "2" in answer.split("\n")[0]
    # والتفصيل يبقى موجوداً حتى يشوف أي طلبات هي.
    assert "ORD-1002" in answer and "ORD-1004" in answer


def test_count_all_orders():
    answer = _answer("كم طلب عدنا بالمجموع؟")
    assert answer is not None and "6" in answer.split("\n")[0]


def test_latest_order():
    """«اخر طلب» — الأحدث وحده، مو القائمة كلها."""
    answer = _answer("شنو اخر طلب؟")
    assert answer is not None
    assert "ORD-1006" in answer
    assert "ORD-1001" not in answer


def test_latest_order_with_status():
    """«اخر طلب قيد التوصيل» — الأحدث ضمن تلك الحالة."""
    answer = _answer("اخر طلب قيد التوصيل")
    assert answer is not None
    assert "ORD-1005" in answer and "ORD-1001" not in answer
