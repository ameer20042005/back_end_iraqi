# -*- coding: utf-8 -*-
"""مكالمة "صباح" لتأجيل التسليم (إضافة على app/features/voice_followup —
انظر prompts.py::SABAH_SYSTEM_PROMPT وrouter.py::decide_turn).

يفحص فقط المنطق الحتمي (بلا نموذج ولا TTS ولا Whisper، نفس فلسفة
test_support_queries.py): استخراج خيار التأجيل من نص الزبون، تحديد
نعم/لا/رقم غلط، حساب تاريخ التأجيل الفعلي، وآلة الحالات الكاملة
(decide_turn) اللي تقود المكالمة من البداية للإغلاق.

التشغيل:  python -m pytest tests/test_voice_followup_postpone.py -v
"""

from datetime import date

from app.features.voice_followup.prompts import option_label
from app.features.voice_followup.router import (
    MAX_POSTPONE_DAYS,
    decide_turn,
    extract_postpone_choice,
    postpone_days,
    resolve_postpone_date,
    _is_no,
    _is_wrong_number,
    _is_yes,
)
from app.features.voice_followup.session_store import MAX_CLARIFY_ATTEMPTS


# ---------------------------------------------------------------------------
# extract_postpone_choice — استخراج الخيار من رد الزبون الحر
# ---------------------------------------------------------------------------

def test_extract_choice_today():
    assert extract_postpone_choice("خلها اليوم لو تكدر") == "today"
    assert extract_postpone_choice("ماكو مشكلة هسه توصلني") == "today"


def test_extract_choice_one_day():
    assert extract_postpone_choice("أجلها بعد يوم") == "plus_1"
    assert extract_postpone_choice("خلها غداً أحسن") == "plus_1"
    assert extract_postpone_choice("باچر أحسن إلي") == "plus_1"


def test_extract_choice_two_days_not_confused_with_one_day():
    # الفحص الحرج: "يومين" تحتوي "يوم" حرفياً، لازم تُصنَّف plus_2 مو plus_1.
    assert extract_postpone_choice("خليها بعد يومين") == "plus_2"
    assert extract_postpone_choice("يومين وتوصلني") == "plus_2"


def test_extract_choice_none_for_unsupported_or_unclear():
    # ما عاد "أسبوع" مرفوضاً (صار plus_7) — المرفوض الآن ما يتجاوز السقف
    # أو ما ماكو بيه موعد أصلاً.
    assert extract_postpone_choice("خليها بعد شهر") is None
    assert extract_postpone_choice("بعد عشرين يوم") is None
    assert extract_postpone_choice("شنو؟ ما فهمت") is None
    assert extract_postpone_choice("") is None


# ---------------------------------------------------------------------------
# مواعيد حرة: عدد أيام، أسبوع، واسم يوم بالأسبوع
# ---------------------------------------------------------------------------


def test_extract_choice_explicit_day_count():
    """عدد الأيام يوصل رقماً إنجليزياً أو هندياً أو منطوقاً حروفاً —
    والثلاثة لازم تعطي نفس المفتاح، لأن Whisper يكتبه بأي شكل منهم."""
    assert extract_postpone_choice("بعد 4 ايام") == "plus_4"
    assert extract_postpone_choice("بعد ٤ ايام") == "plus_4"
    assert extract_postpone_choice("خليها بعد اربع ايام") == "plus_4"
    assert extract_postpone_choice("بعد عشره ايام") == "plus_10"


def test_extract_choice_weeks():
    assert extract_postpone_choice("خليها بعد اسبوع") == "plus_7"
    assert extract_postpone_choice("بعد اسبوعين") == "plus_14"


def test_extract_choice_day_after_tomorrow_beats_tomorrow():
    """«بعد غدا» تحتوي «غدا» حرفياً — لولا ترتيب الفحص لصارت plus_1."""
    assert extract_postpone_choice("بعد غدا") == "plus_2"
    assert extract_postpone_choice("بعد بكره") == "plus_2"
    assert extract_postpone_choice("بكره") == "plus_1"


def test_extract_choice_weekday_names():
    assert extract_postpone_choice("خليها الخميس") == "weekday_3"
    assert extract_postpone_choice("يوم الاثنين") == "weekday_0"


def test_weekday_resolves_to_next_occurrence():
    """السبت 2026-09-05: «الخميس» بعده بخمسة أيام، و«السبت» نفسه يعني
    السبت الجاي (سبعة أيام) لا اليوم — الزبون اللي يگول اسم يوم اليوم
    نفسه يقصد الأسبوع الجاي."""
    saturday = date(2026, 9, 5)
    assert resolve_postpone_date("weekday_3", saturday) == "2026-09-10"
    assert resolve_postpone_date("weekday_5", saturday) == "2026-09-12"
    assert postpone_days("weekday_5", saturday) == 7


def test_choice_above_cap_is_rejected():
    """فوق السقف يرجع None فتعيد صباح السؤال — ما ينثبّت موعد مستحيل."""
    assert extract_postpone_choice(f"بعد {MAX_POSTPONE_DAYS} ايام") == f"plus_{MAX_POSTPONE_DAYS}"
    assert extract_postpone_choice(f"بعد {MAX_POSTPONE_DAYS + 1} ايام") is None


def test_option_label_echoes_customer_phrasing():
    """التسمية ترجع بصيغة الزبون: اسم اليوم يبقى اسم يوم، ما ينقلب
    لعدد أيام — وإلا جملة التأكيد تبدو كأن صباح ما فهمته."""
    assert option_label("weekday_3", 5) == "يوم الخميس"
    assert option_label("today", 0) == "اليوم"
    assert option_label("plus_2", 2) == "بعد يومين"
    assert option_label("plus_7", 7) == "بعد أسبوع"
    assert option_label("plus_4", 4) == "بعد 4 أيام"


# ---------------------------------------------------------------------------
# نعم/لا/رقم غلط — مطابقة بحدود كلمة، بلا مطابقات كاذبة
# ---------------------------------------------------------------------------

def test_yes_no_detection():
    assert _is_yes("ايه تمام خلها هيچي") is True
    assert _is_no("ايه تمام خلها هيچي") is False
    assert _is_no("لا لا ما اريدها هيچي") is True
    assert _is_yes("لا لا ما اريدها هيچي") is False


def test_no_false_positive_inside_longer_word():
    # "لازم" تحتوي "لا" كسابقة بس بلا حدود كلمة — ما لازم تُحتسب "لا".
    assert _is_no("لازم اسولف وياك بعدين") is False


def test_wrong_number_detection():
    assert _is_wrong_number("هذا مو طلبي والله") is True
    assert _is_wrong_number("تمام خلها اليوم") is False


# ---------------------------------------------------------------------------
# resolve_postpone_date — تاريخ ISO فعلي من الخيار المؤكَّد
# ---------------------------------------------------------------------------

def test_resolve_postpone_date_offsets():
    today = date(2026, 9, 3)
    assert resolve_postpone_date("today", today) == "2026-09-03"
    assert resolve_postpone_date("plus_1", today) == "2026-09-04"
    assert resolve_postpone_date("plus_2", today) == "2026-09-05"


# ---------------------------------------------------------------------------
# decide_turn — آلة الحالات الكاملة اللي تقود المكالمة
# ---------------------------------------------------------------------------

def test_choice_then_confirmation_flow():
    d1 = decide_turn("awaiting_choice", "خلها بعد يوم", None, 0)
    assert (d1.new_state, d1.reply_case, d1.chosen) == ("awaiting_confirmation", "confirm_choice", "plus_1")

    d2 = decide_turn("awaiting_confirmation", "ايه تمام", d1.chosen, d1.new_attempts)
    assert (d2.new_state, d2.reply_case) == ("closed", "confirmed")


def test_confirmation_rejected_resets_choice():
    d = decide_turn("awaiting_confirmation", "لا لا خلها غيرها", "plus_1", 0)
    assert (d.new_state, d.reply_case, d.chosen, d.new_attempts) == ("awaiting_choice", "reset_choice", None, 0)


def test_unclear_reply_escalates_then_gives_up():
    state, attempts = "awaiting_choice", 0
    for _ in range(MAX_CLARIFY_ATTEMPTS):
        d = decide_turn(state, "شنو؟ ما فهمت", None, attempts)
        assert d.reply_case == "clarify"
        assert d.new_state == "awaiting_choice"
        state, attempts = d.new_state, d.new_attempts

    d_final = decide_turn(state, "شنو؟ ما فهمت", None, attempts)
    assert (d_final.new_state, d_final.reply_case) == ("closed", "give_up")


def test_silence_follows_same_escalation_as_unclear_reply():
    # transcribe() يرجّع "" (سلسلة فاضية) لو ماكو كلام مفهوم بالملف — لازم
    # تسلك نفس مسار الرد الغامض، بلا فرع خاص.
    d = decide_turn("awaiting_choice", "", None, 0)
    assert d.reply_case == "clarify"


def test_wrong_number_closes_regardless_of_state():
    d1 = decide_turn("awaiting_choice", "هذا مو طلبي", None, 0)
    assert (d1.new_state, d1.reply_case) == ("closed", "wrong_number")

    d2 = decide_turn("awaiting_confirmation", "رقم غلط اتصلتوا", "today", 1)
    assert (d2.new_state, d2.reply_case) == ("closed", "wrong_number")
