# -*- coding: utf-8 -*-
"""العقوبة المخفَّفة لحارس الأرقام (app/guards.py: redact_bad_numbers).

الخلفية: العقوبة القديمة كانت **مسح الرد كله** واستبداله برد تهرب ثابت. بلقطة
إنتاج حقيقية، سعة التخزين «SSD 512» انقرأت سعراً مختلَقاً فانمسح ملخّص الطلب
كامل — والزبون گال «نعم اكد» ثلاث مرات بلا ما ينثبت طلبه. إنذار كاذب واحد كان
يقطع عملية بيع كاملة.

التنقيح يحافظ على المبدأ (ما يمر ولا رقم مختلَق) بلا ما يقطع المحادثة.

الاختبارات هنا تحرس اتجاهين معاً:
  ١. ما ينمسح رقم سليم (وإلا رجعنا للإنذارات الكاذبة).
  ٢. ما يفلت رقم مختلَق بأي صيغة كتابة — وهذا الأهم: تسريب صامت أسوأ من
     الإنذار الكاذب اللي نعالجه.

التشغيل:  python -m pytest tests/test_redaction.py -v
"""

import pytest

from app.guards import _REDACTION, redact_bad_numbers

_CATALOG = "- لابتوب لينوفو IdeaPad 15 | السعر: 750000 IQD | رام 8 جيجا، تخزين 512 SSD."

# ملخّص الطلب الحقيقي من لقطة الإنتاج.
_ORDER_SUMMARY = (
    "خوش، هذا هو طلبك: لابتوب لينوفو IdeaPad 15 (رام 8 جيجا، تخزين SSD 512) "
    "عدد 1 بـ750,000 دينار، باسم امير وسام عبد الستار ورقم هاتفك 07811109151 "
    "والتوصيل لحي موسى الكاظم السماوة. تأكدلي الطلب اسوي؟"
)


# --------------------------------------------------------------------------
# ١. الردود السليمة تمر بلا أي مساس
# --------------------------------------------------------------------------

CLEAN_CASES = [
    ("ملخّص الطلب الحقيقي", _ORDER_SUMMARY),
    ("سعر الكتالوج", "سعره 750,000 دينار حبيبي"),
    ("سعر بلا فواصل", "سعره 750000 دينار"),
    ("مواصفات تقنية", "تخزين SSD 512 ورام 8 جيجا"),
    ("رقم هاتف", "رقمك 07811109151 صح؟"),
    ("مجاملة", "مليون شكر حبيبي"),
    ("كمية ومدة", "عدد 2 خلال 3 أيام"),
]


@pytest.mark.parametrize(
    "description,reply", CLEAN_CASES, ids=[c[0] for c in CLEAN_CASES]
)
def test_clean_reply_is_untouched(description, reply):
    """الرد السليم يرجع حرفياً كما هو — ولا حرف يتغير."""
    redacted, removed = redact_bad_numbers(reply, _CATALOG)
    assert removed == [], description
    assert redacted == reply, description


def test_order_summary_survives_intact():
    """هذا العطل الأصلي: الملخّص كان ينمسح كله فينكسر التثبيت."""
    redacted, removed = redact_bad_numbers(_ORDER_SUMMARY, _CATALOG)
    assert removed == []
    assert "امير وسام عبد الستار" in redacted
    assert "07811109151" in redacted
    assert "تأكدلي الطلب اسوي؟" in redacted


# --------------------------------------------------------------------------
# ٢. الأرقام المختلَقة تُمسح بكل صيغة كتابة
# --------------------------------------------------------------------------

BAD_CASES = [
    ("سعر بفواصل", "أنطيك ياه بـ600,000 دينار", "600,000"),
    ("سعر بلا فواصل", "أنطيك ياه بـ600000 دينار", "600000"),
    ("أرقام عربية-هندية", "السعر ٦٠٠٠٠٠ دينار", "٦٠٠٠٠٠"),
    ("هندية بفواصل", "السعر ٦٠٠,٠٠٠ دينار", "٦٠٠,٠٠٠"),
    ("صيغة مختصرة", "أنطيك ياه بـ900 الف دينار", "900"),
    ("سعر بالحروف", "أنطيك ياه بمليون دينار", "مليون"),
    ("نسبة مئوية", "أنطيك 5% خصم", "5"),
]


@pytest.mark.parametrize(
    "description,reply,leaked",
    BAD_CASES,
    ids=[c[0] for c in BAD_CASES],
)
def test_invented_number_is_removed(description, reply, leaked):
    """الرقم المختلَق ما يوصل العميل بأي صيغة — تسريب صامت أسوأ عطل ممكن."""
    redacted, removed = redact_bad_numbers(reply, _CATALOG)
    assert removed, f"{description}: ما انمسك الرقم إطلاقاً"
    assert leaked not in redacted, f"{description}: الرقم تسرّب للعميل"
    assert _REDACTION in redacted, description


def test_currency_word_removed_with_the_number():
    """العملة تنمسح وية الرقم، وإلا بقت معلّقة («أتأكدلك من السعر دينار»)."""
    redacted, _ = redact_bad_numbers("أنطيك ياه بـ600,000 دينار", _CATALOG)
    assert "دينار" not in redacted


def test_percent_sign_removed_with_the_number():
    """علامة النسبة تنمسح وياه — كانت تبقى معلّقة بعد التنقيح."""
    redacted, _ = redact_bad_numbers("أنطيك 5% خصم", _CATALOG)
    assert "%" not in redacted


# --------------------------------------------------------------------------
# ٣. باقي الرد يبقى سليماً حتى لما يُمسح رقم
# --------------------------------------------------------------------------


def test_rest_of_reply_survives_redaction():
    """الفرق الجوهري عن العقوبة القديمة: بيانات العميل ما تضيع وية السعر."""
    reply = (
        "طلبك: لابتوب لينوفو عدد 1 بـ600,000 دينار، باسم امير وسام "
        "ورقم هاتفك 07811109151 والتوصيل للسماوة. تأكدلي؟"
    )
    redacted, removed = redact_bad_numbers(reply, _CATALOG)
    assert removed == ["600,000"]
    assert "600,000" not in redacted
    # كل ما عدا السعر باقٍ — هذا اللي كان ينمسح كله سابقاً.
    for fragment in ("امير وسام", "07811109151", "للسماوة", "تأكدلي؟", "لابتوب لينوفو"):
        assert fragment in redacted, f"ضاع من الرد: {fragment}"


def test_valid_price_kept_while_invented_one_removed():
    """رد فيه سعر صحيح وآخر مختلَق: ينمسح المختلَق وحده."""
    redacted, removed = redact_bad_numbers(
        "اللابتوب بـ750,000 دينار والحقيبة بـ99,000 دينار", _CATALOG
    )
    assert "750,000" in redacted
    assert "99,000" not in redacted
    assert removed == ["99,000"]


def test_multiple_invented_numbers_all_removed():
    redacted, removed = redact_bad_numbers(
        "واحد بـ600,000 وواحد بـ880,000 دينار", _CATALOG
    )
    assert len(removed) == 2
    assert "600,000" not in redacted and "880,000" not in redacted
