# -*- coding: utf-8 -*-
"""مكالمة "صباح" لتأجيل التسليم (إضافة على app/features/voice_followup —
انظر prompts.py::SABAH_SYSTEM_PROMPT وrouter.py::decide_turn).

**ما تغيّر بهذا الملف بعد نقل الفهم اللغوي للنموذج:**

كانت أغلب الاختبارات هنا تفحص مطابقة نصية على جداول كلمات ("باجر" →
plus_1، "بعد اسبوعين" → plus_14...). تلك الجداول انحذفت وصار النموذج يفهم
اللغة، فاختبارها هنا ما عاد ممكناً ولا مفيداً — تقييم نموذج لغوي يحتاج
مجموعة تقييم منفصلة، لا وحدات pytest حتمية.

**وما بقي قابلاً للاختبار حتمياً — وهو الأهم أمنياً:**
  ١. `parse_understanding` — الحدّ الفاصل بين مخرَج النموذج والخادم. هذي
     الدالة هي كل ما يمنع تاريخاً غلطاً من الوصول لباك اند السستم.
  ٢. `POSTPONE_CHOICES` / `TURN_SCHEMA` — أن السقف مفروض بالجدول نفسه.
  ٣. `decide_turn` — آلة الحالات، لا زالت حتمية بالكامل بلا أي نموذج.
  ٤. حساب التواريخ (`postpone_days` / `resolve_postpone_date`).

التشغيل:  python -m pytest tests/test_voice_followup_postpone.py -v
"""

from datetime import date

from app.features.voice_followup.prompts import (
    build_ask_prompt,
    build_turn_understanding_prompt,
    option_label,
    option_label_ku,
)
from app.lang import Lang
from app.features.voice_followup.schema import VoiceFollowupOrderRequest
from app.features.voice_followup.router import (
    MAX_POSTPONE_DAYS,
    POSTPONE_CHOICES,
    TURN_SCHEMA,
    Understanding,
    decide_turn,
    parse_understanding,
    postpone_days,
    resolve_postpone_date,
    _postpone_fallback,
)
from app.features.voice_followup.session_store import MAX_CLARIFY_ATTEMPTS


def test_followup_prompt_never_receives_customer_name():
    """اسم الشخص يبقى خارج سياق نموذج الصوت، فلا يمكن أن ينطقه بالمكالمة."""
    order = VoiceFollowupOrderRequest(
        order_id="ORD-1001",
        status="ملغي",
        customer_name="أمير وسام",
        items=[],
    )

    prompt_text = "\n".join(message["content"] for message in build_ask_prompt(order))

    assert "أمير" not in prompt_text
    assert "وسام" not in prompt_text


# ---------------------------------------------------------------------------
# جدول الخيارات — السقف مفروض بالجدول لا بشرط منفصل
# ---------------------------------------------------------------------------

def test_choices_are_generated_from_the_cap():
    """الجدول مشتق من MAX_POSTPONE_DAYS: تغيير السقف يغيّر الجدول تلقائياً،
    فما يصير عندنا مصدرا حقيقة يفترقان بصمت."""
    assert "today" in POSTPONE_CHOICES
    assert f"plus_{MAX_POSTPONE_DAYS}" in POSTPONE_CHOICES
    assert f"plus_{MAX_POSTPONE_DAYS + 1}" not in POSTPONE_CHOICES
    assert all(f"weekday_{d}" in POSTPONE_CHOICES for d in range(7))
    assert len(POSTPONE_CHOICES) == 1 + MAX_POSTPONE_DAYS + 7


def test_schema_enum_matches_the_choice_table():
    """المخطط المرسَل لـvLLM يطابق الجدول حرفياً — قيمة خارجه تعني أن
    النموذج يقدر يرجّع مفتاحاً ما يعرف الخادم يحسبه."""
    enum = TURN_SCHEMA["properties"]["choice"]["enum"]
    assert set(enum) == set(POSTPONE_CHOICES) | {"none"}


def test_every_choice_in_the_table_is_computable():
    """كل مفتاح بالجدول لازم postpone_days تعرف تحسبه وما يتجاوز السقف —
    يمسك أي مفتاح يُضاف للجدول بلا ما يُضاف لمنطق الحساب."""
    today = date(2026, 9, 3)
    for choice in POSTPONE_CHOICES:
        days = postpone_days(choice, today)
        assert 0 <= days <= MAX_POSTPONE_DAYS, (choice, days)


# ---------------------------------------------------------------------------
# parse_understanding — الحدّ الفاصل بين النموذج والخادم
# ---------------------------------------------------------------------------

def test_valid_understanding_passes_through():
    u = parse_understanding('{"intent": "choice", "choice": "plus_3"}')
    assert (u.intent, u.choice) == ("choice", "plus_3")


def test_choice_outside_the_table_is_rejected():
    """حتى لو تجاوز النموذج المخطط (إصدار vLLM تغيّر، أو مسار بلا تقييد)،
    الخادم ما يقبل مفتاحاً خارج الجدول — يسقط لـunclear فتعيد صباح السؤال
    بدل ما يُحفظ تاريخ ما ينتمي لأي خيار مسموح."""
    assert parse_understanding('{"intent": "choice", "choice": "plus_99"}').intent == "unclear"
    assert parse_understanding('{"intent": "choice", "choice": "غدا"}').intent == "unclear"
    assert parse_understanding('{"intent": "choice", "choice": "none"}').intent == "unclear"


def test_choice_is_ignored_unless_intent_is_choice():
    """رد يقول "نعم" ويحمل موعداً: النية تُقبل والموعد يُتجاهَل — ما نخلي
    النموذج يثبّت موعداً بدور مخصَّص للتأكيد."""
    u = parse_understanding('{"intent": "yes", "choice": "plus_3"}')
    assert (u.intent, u.choice) == ("yes", None)


def test_malformed_output_degrades_to_unclear():
    """أي شذوذ — JSON غير صالح، نوع غلط، نية مجهولة، رد فاضٍ — يسقط لمسار
    إعادة السؤال بدل ما يرمي استثناء يقطع المكالمة على الزبون."""
    for raw in ("ليس JSON", "", None, "[]", '"نص"', '{"intent": "maybe", "choice": "today"}', "{}"):
        assert parse_understanding(raw).intent == "unclear", raw


# ---------------------------------------------------------------------------
# برومبت الفهم
# ---------------------------------------------------------------------------

def test_understanding_prompt_carries_state_and_transcript():
    """الحالة تدخل البرومبت لأن "تمام" تحمل معنيين مختلفين حسب موضعها."""
    messages = build_turn_understanding_prompt("خليها باجر", "awaiting_confirmation")
    text = "\n".join(m["content"] for m in messages)
    assert "خليها باجر" in text
    assert "تأكيداً" in text


# ---------------------------------------------------------------------------
# التسمية المنطوقة وحساب التواريخ — حتمية بالكامل
# ---------------------------------------------------------------------------

def test_option_label_echoes_customer_phrasing():
    """التسمية ترجع بصيغة الزبون: اسم اليوم يبقى اسم يوم، ما ينقلب
    لعدد أيام — وإلا جملة التأكيد تبدو كأن صباح ما فهمته."""
    assert option_label("weekday_3", 5) == "يوم الخميس"
    assert option_label("today", 0) == "اليوم"
    # "باجر"/"بعد باجر" — نفس كلمات العرض بالبرومبت، لا "بعد يوم"/"بعد يومين".
    assert option_label("plus_1", 1) == "باجر"
    assert option_label("plus_2", 2) == "بعد باجر"
    assert option_label("plus_7", 7) == "بعد أسبوع"
    assert option_label("plus_4", 4) == "بعد 4 أيام"


def test_kurdish_fallback_keeps_the_reply_and_date_label_in_sorani():
    """لما vLLM غير متاح، الراوتر هو من يكتب الرد؛ لذلك لازم لغة
    fallback تُستمد من رد الزبون ولا تعيد صباح للعربية."""
    assert option_label_ku("plus_1", 1) == "سبەینێ"
    answer = _postpone_fallback("confirmed", "plus_1", Lang.KU)
    assert answer == "باشە، بۆ سبەینێ دایدەنین. سوپاس بۆ کاتت، خوات لەگەڵ."


def test_weekday_resolves_to_next_occurrence():
    """السبت 2026-09-05: «الخميس» بعده بخمسة أيام، و«السبت» نفسه يعني
    السبت الجاي (سبعة أيام) لا اليوم — الزبون اللي يگول اسم يوم اليوم
    نفسه يقصد الأسبوع الجاي."""
    saturday = date(2026, 9, 5)
    assert resolve_postpone_date("weekday_3", saturday) == "2026-09-10"
    assert resolve_postpone_date("weekday_5", saturday) == "2026-09-12"
    assert postpone_days("weekday_5", saturday) == 7


def test_resolve_postpone_date_offsets():
    today = date(2026, 9, 3)
    assert resolve_postpone_date("today", today) == "2026-09-03"
    assert resolve_postpone_date("plus_1", today) == "2026-09-04"
    assert resolve_postpone_date("plus_2", today) == "2026-09-05"


# ---------------------------------------------------------------------------
# decide_turn — آلة الحالات، لا زالت حتمية بلا أي نموذج
# ---------------------------------------------------------------------------

_UNCLEAR = Understanding("unclear", None)


def test_choice_then_confirmation_flow():
    d1 = decide_turn("awaiting_choice", Understanding("choice", "plus_1"), None, 0)
    assert (d1.new_state, d1.reply_case, d1.chosen) == (
        "awaiting_confirmation", "confirm_choice", "plus_1",
    )

    d2 = decide_turn("awaiting_confirmation", Understanding("yes", None), d1.chosen, d1.new_attempts)
    assert (d2.new_state, d2.reply_case) == ("closed", "confirmed")


def test_confirmation_rejected_resets_choice():
    d = decide_turn("awaiting_confirmation", Understanding("no", None), "plus_1", 0)
    assert (d.new_state, d.reply_case, d.chosen, d.new_attempts) == (
        "awaiting_choice", "reset_choice", None, 0,
    )


def test_unclear_reply_escalates_then_gives_up():
    state, attempts = "awaiting_choice", 0
    for _ in range(MAX_CLARIFY_ATTEMPTS):
        d = decide_turn(state, _UNCLEAR, None, attempts)
        assert d.reply_case == "clarify"
        assert d.new_state == "awaiting_choice"
        state, attempts = d.new_state, d.new_attempts

    d_final = decide_turn(state, _UNCLEAR, None, attempts)
    assert (d_final.new_state, d_final.reply_case) == ("closed", "give_up")


def test_silence_follows_same_escalation_as_unclear_reply():
    """سكوت الزبون يوصل كـintent="unclear" (transcribe يرجّع "" فما اكو شي
    يفهمه النموذج) — يسلك نفس مسار الرد الغامض بلا فرع خاص."""
    d = decide_turn("awaiting_choice", _UNCLEAR, None, 0)
    assert d.reply_case == "clarify"


def test_wrong_number_closes_regardless_of_state():
    wrong = Understanding("wrong_number", None)
    d1 = decide_turn("awaiting_choice", wrong, None, 0)
    assert (d1.new_state, d1.reply_case) == ("closed", "wrong_number")

    d2 = decide_turn("awaiting_confirmation", wrong, "today", 1)
    assert (d2.new_state, d2.reply_case) == ("closed", "wrong_number")


def test_choice_without_key_does_not_confirm():
    """intent="choice" بلا مفتاح صالح (بعد ما رفضه parse_understanding) ما
    يثبّت شي — ينسحب لإعادة السؤال. يمنع تثبيت موعد فاضٍ."""
    d = decide_turn("awaiting_choice", Understanding("choice", None), None, 0)
    assert d.reply_case == "clarify"
