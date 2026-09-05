# -*- coding: utf-8 -*-
"""مسار المتابعة الصوتية للطلبات: POST /voice_followup/ask و
POST /voice_followup/respond.

المسار كامل (انظر تصميم الميزة):
1) باك اند السستم يرسل تفاصيل طلب (رقمه، حالته، الزبون، المنتجات) لـ /ask.
2) نولّد سؤالاً عراقياً طبيعياً حسب الحالة (النموذج النصي) ونحوّله لصوت
   (F5-TTS بصوت مرجعي — app/features/voice_followup/tts.py)، ونرجعه مع
   session_id تُخزَّن بيه معطيات الطلب (app/features/voice_followup/
   session_store.py).
3) باك اند السستم يشغّل الصوت للزبون، يسجّل رده، ويرسله لـ /respond مع
   نفس session_id.
4) نحوّل رد الزبون الصوتي لنص (Whisper — نفس محرك order_intake)، نلخّص
   السبب بالنموذج، نرسل query كامل (بيانات الزبون من الطلب الأصلي + السبب)
   لباك اند السستم (app/features/voice_followup/gateway.py)، ونرجع صوت شكر
   جاهز للتشغيل مباشرة للزبون — التفاصيل (النص، الملخّص، هل انرسل الـ query)
   تصل بهيدرات الرد حتى يبقى جسم الرد ملف صوت خام صالح للتشغيل فوراً.

بلا أي تخزين محلي دائم — الجلسة تعيش بالذاكرة فقط بين الخطوتين 2 و3."""

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.auth import require_voice_followup_api_key
from app.engine import llm_engine
from app.features.order_intake.transcribe import transcribe
from app.features.voice_followup import session_store, tts
from app.features.voice_followup.gateway import voice_followup_submitter, voice_postpone_submitter
from app.features.voice_followup.prompts import (
    option_label,
    build_analyze_prompt,
    build_ask_prompt,
    build_postpone_dialogue_prompt,
    build_postpone_opening_prompt,
)
from app.features.voice_followup.schema import VoiceFollowupOrderRequest
from app.system_backend import SystemBackendUnavailable
from app.text_norm import normalize

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice_followup", tags=["voice_followup"])

_SESSION_HEADER = "X-Session-Id"

# ردود احتياطية حتمية لو الموديل النصي غير جاهز (محلياً بدون GPU) — نصوص
# عامة محايدة تصلح لأي حالة طلب، بدل ما نمنع الميزة كاملة.
_FALLBACK_QUESTION = "هلا بيك، عدنا استفسار عن طلبك — شنو سبب الحالة الحالية لطلبك، لو سمحت؟"
_FALLBACK_THANKS = "تسلم حبيبي على وقتك، راح نراجع الموضوع ونرجعلك."
_FALLBACK_REASON = "الزبون لم يذكر سبباً واضحاً"


def _ascii_header(value: str) -> str:
    """هيدرات HTTP لازم Latin-1 — نص عربي بيها يكسر الاستجابة. نرمّزها
    percent-encoding (RFC 5987) حتى يوصل النص كاملاً بلا فقدان، والمستدعي
    يفك ترميزه بجهته (urllib.parse.unquote) إذا احتاج القراءة المباشرة."""
    return quote(value)


async def _generate_question_text(order: VoiceFollowupOrderRequest) -> str:
    if not llm_engine.ready:
        return _FALLBACK_QUESTION
    messages = build_ask_prompt(order)
    text = await llm_engine.generate_full(
        llm_engine.render_prompt(messages), max_tokens=160, temperature=0.0,
    )
    return text.strip() or _FALLBACK_QUESTION


async def _analyze_customer_reply(order: VoiceFollowupOrderRequest, transcript: str) -> str:
    if not llm_engine.ready:
        return _FALLBACK_REASON
    messages = build_analyze_prompt(order, transcript)
    text = await llm_engine.generate_full(
        llm_engine.render_prompt(messages), max_tokens=120, temperature=0.0,
    )
    return text.strip() or _FALLBACK_REASON


def _synthesize_or_503(text: str) -> bytes:
    audio = tts.synthesize(text)
    if audio is None:
        raise HTTPException(503, "تحويل النص لصوت غير متوفر محلياً (يحتاج f5-tts مثبَّتة).")
    return audio


# ---------------------------------------------------------------------------
# مكالمة "صباح" (تأجيل التسليم) — منطق حتمي كامل، بلا أي قرار من النموذج
# ---------------------------------------------------------------------------
#
# نفس فلسفة app/features/support/router.py: كل قرار (هل الرد يطابق خياراً؟
# هل انتهت المكالمة؟) يُحسب هنا بمطابقة نصية صريحة على بيانات حقيقية (رد
# الزبون المحوَّل من صوت لنص) — النموذج (انظر _generate_postpone_reply
# أدناه) يُستدعى فقط بعد ما يُحسم القرار، ليصوغه بلهجة عراقية طبيعية.

# الحد الأقصى للتأجيل. سقف ضروري لسببين: يمنع قيماً عبثية ("أجلها سنة")
# تصير التزاماً تشغيلياً ما ننفّذه، ويمنع خطأ Whisper برقم منطوق (يسمع
# "أربعين" بدل "أربعة") من تحويل مكالمة عادية لتأجيل مستحيل. أي رقم فوقه
# يُعامَل كخيار غير مسموح، فتعيد صباح السؤال بدل ما تثبّته.
MAX_POSTPONE_DAYS = 14

# ترتيب الفحص أدناه **حرج**: نطابق الأطول والأخص أولاً. "بعد غدا" تحتوي
# "غدا"، و"يومين" تحتوي "يوم"، و"اسبوعين" تحتوي "اسبوع" — فحص المختصر
# أولاً يصنّف الطويل غلط. نفس تحذير extract_status بـsupport/router.py.
#
# كل القوائم مكتوبة بصيغتها **المطبَّعة** (بعد normalize): بلا همزات ولا
# تاء مربوطة ("غداً"→"غدا"، "بكرة"→"بكره"، "أسبوع"→"اسبوع").
_DAY_AFTER_TOMORROW_WORDS = ("بعد غدا", "بعد باچر", "بعد بكره", "عقب غدا", "عقب باچر")
_TWO_DAYS_WORDS = ("يومين", "بيومين")
_TWO_WEEKS_WORDS = ("اسبوعين", "باسبوعين")
_ONE_WEEK_WORDS = ("اسبوع", "باسبوع", "جمعه")
_TOMORROW_WORDS = ("غدا", "باچر", "بكره", "بجر")
_ONE_DAY_WORDS = ("بعد يوم", "يوم واحد", "بيوم")
_TODAY_WORDS = ("اليوم", "هسه", "نفس اليوم", "هذا اليوم", "الحين", "هلحين")

# أسماء الأعداد المنطوقة — Whisper يكتبها حروفاً لا أرقاماً غالباً
# ("بعد أربعة أيام" لا "بعد 4 أيام")، فلازم نغطي الشكلين.
_NUMBER_WORDS = {
    "يوم": 1, "يومين": 2, "ثلاث": 3, "ثلاثه": 3, "تلاث": 3, "تلاته": 3,
    "اربع": 4, "اربعه": 4, "خمس": 5, "خمسه": 5, "ست": 6, "سته": 6,
    "سبع": 7, "سبعه": 7, "ثمان": 8, "ثمانيه": 8, "تمن": 8, "تمانيه": 8,
    "تسع": 9, "تسعه": 9, "عشر": 10, "عشره": 10,
}

# أيام الأسبوع → ترقيم date.weekday() (الاثنين=0 … الأحد=6).
_WEEKDAYS = {
    "الاثنين": 0, "الاثنين": 0, "الثلاثاء": 1, "الاربعاء": 2,
    "الخميس": 3, "الجمعه": 4, "السبت": 5, "الاحد": 6,
    "اثنين": 0, "ثلاثاء": 1, "اربعاء": 2, "خميس": 3, "جمعه": 4, "سبت": 5, "احد": 6,
}

# "بعد ٤ ايام" / "بعد 4 يوم" — الأرقام وصلت إنجليزية بعد normalize.
_DIGIT_DAYS_RE = re.compile(r"\b(\d{1,2})\s*(?:ايام|يوم|يوما)\b")
# "بعد اربع ايام" — عدد منطوق متبوعاً بكلمة أيام.
_WORD_DAYS_RE = re.compile(r"\b(" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")\s*(?:ايام|يوم|يوما)\b")

_YES_WORDS = ("نعم", "ايوه", "ايه", "اي", "تمام", "زين", "موافق", "صح", "اوكي")
_NO_WORDS = ("لا", "كلا", "ماريد", "ما اريد", "تراجعت", "غيرها", "بديل")

# رقم غلط / شخص غير مقصود — أولوية قصوى بغض النظر عن حالة المكالمة (انظر
# decide_turn أدناه): تُقفل المكالمة فوراً بلا محاولة متابعة الموضوع.
_WRONG_NUMBER_HINTS = ("مو طلبي", "رقم غلط", "غلط الرقم", "منو تريد", "ماعندي طلب", "خطا الرقم", "غلط رقم")


def _contains_word(normalized: str, words: "tuple[str, ...]") -> bool:
    """مطابقة بحدود كلمة (\\b) — يمنع مطابقة كاذبة لكلمات قصيرة مثل "لا"
    داخل كلمة أطول ("لازم")، خلافاً لفحص substring بسيط."""
    return any(re.search(rf"\b{re.escape(w)}\b", normalized) for w in words)


def _days_choice(days: int) -> Optional[str]:
    """يحوّل عدد أيام لمفتاح خيار، أو None لو تجاوز السقف أو كان سالباً.
    الصفر يصير "today" حتى يبقى مفتاح اليوم واحداً بكل المسارات."""
    if days < 0 or days > MAX_POSTPONE_DAYS:
        return None
    return "today" if days == 0 else f"plus_{days}"


def extract_postpone_choice(message: str) -> Optional[str]:
    """يستخرج خيار التأجيل من رد الزبون الحر (المحوَّل من صوت لنص).

    الصيغ المدعومة:
      - اليوم / هسه            → "today"
      - غدا / باچر / بكره      → "plus_1"
      - بعد غدا / بعد يومين    → "plus_2"
      - بعد N أيام (رقماً أو حروفاً، لين MAX_POSTPONE_DAYS) → "plus_N"
      - أسبوع / أسبوعين        → "plus_7" / "plus_14"
      - اسم يوم بالأسبوع       → "weekday_D" (يُحسَب تاريخه لاحقاً)

    يرجع None لرد غامض، أو موعد خارج السقف — وكلاهما يقود صباح لإعادة
    السؤال بدل الحسم (انظر decide_turn).

    ليش "weekday_D" ما ينحسب هنا لتاريخ مباشرة؟ لأن الحساب يحتاج تاريخ
    اليوم، وتثبيته لحظة الاستخراج يخلي الدالة غير قابلة للاختبار بتاريخ
    ثابت — نأجّله لـresolve_postpone_date اللي تستقبل `today` صراحة.
    """
    normalized = normalize(message)

    # 1) بعد غد — قبل "غدا" وقبل الأرقام، لأنها تحتوي "غدا" حرفياً.
    if any(w in normalized for w in _DAY_AFTER_TOMORROW_WORDS):
        return "plus_2"

    # 2) أسبوعان ثم أسبوع — "اسبوعين" تحتوي "اسبوع".
    if any(w in normalized for w in _TWO_WEEKS_WORDS):
        return _days_choice(14)

    # 3) عدد صريح بالأرقام، ثم عدد منطوق حروفاً. يسبق باقي الكلمات لأن
    #    "بعد يومين" تُطابق هنا برقم 2 بنفس النتيجة، بينما "بعد اربع ايام"
    #    ما تطابق أي كلمة ثابتة.
    m = _DIGIT_DAYS_RE.search(normalized)
    if m:
        return _days_choice(int(m.group(1)))
    m = _WORD_DAYS_RE.search(normalized)
    if m:
        return _days_choice(_NUMBER_WORDS[m.group(1)])

    # 4) يومان بصيغته المجردة ("خليها يومين").
    if any(w in normalized for w in _TWO_DAYS_WORDS):
        return "plus_2"

    # 5) أسبوع مجرد. "جمعه" تعني أسبوعاً بالعراقي، وتُفحص هنا لا مع أيام
    #    الأسبوع أدناه لأن "بعد جمعه" أشيع من قصد يوم الجمعة نفسه.
    if any(w in normalized for w in _ONE_WEEK_WORDS):
        return _days_choice(7)

    # 6) اسم يوم بالأسبوع — بعد الأرقام حتى "بعد اربع ايام" ما تنسحب هنا.
    for name, weekday in _WEEKDAYS.items():
        if re.search(rf"\b{re.escape(name)}\b", normalized):
            return f"weekday_{weekday}"

    # 7) غداً ثم "بعد يوم" ثم اليوم — الأقصر والأعم آخراً.
    if any(w in normalized for w in _TOMORROW_WORDS):
        return "plus_1"
    if any(w in normalized for w in _ONE_DAY_WORDS):
        return "plus_1"
    if any(w in normalized for w in _TODAY_WORDS):
        return "today"
    return None


def _is_yes(message: str) -> bool:
    return _contains_word(normalize(message), _YES_WORDS)


def _is_no(message: str) -> bool:
    return _contains_word(normalize(message), _NO_WORDS)


def _is_wrong_number(message: str) -> bool:
    normalized = normalize(message)
    return any(h in normalized for h in _WRONG_NUMBER_HINTS)


def postpone_days(choice: str, today: Optional[date] = None) -> int:
    """عدد الأيام من اليوم لموعد الخيار. مفصولة عن resolve_postpone_date
    لأن التسمية العربية (option_label) تحتاج العدد لا التاريخ."""
    if choice == "today":
        return 0
    if choice.startswith("plus_"):
        return int(choice[len("plus_"):])
    if choice.startswith("weekday_"):
        today = today or date.today()
        target = int(choice[len("weekday_"):])
        delta = (target - today.weekday()) % 7
        # صفر يعني إن اليوم نفسه هو اليوم المطلوب — والزبون اللي يگول
        # "خليها الخميس" وهو يوم خميس يقصد الخميس الجاي، لا اليوم.
        return delta or 7
    raise ValueError(f"خيار تأجيل غير معروف: {choice!r}")


def resolve_postpone_date(choice: str, today: Optional[date] = None) -> str:
    """يحسب تاريخ ISO الفعلي لخيار تأجيل مؤكَّد. `today` قابلة للتمرير
    للاختبار (تاريخ ثابت بدل تاريخ التشغيل الفعلي) — نفس نمط
    support/router.py::_resolve_relative_range."""
    today = today or date.today()
    return (today + timedelta(days=postpone_days(choice, today))).isoformat()


@dataclass
class TurnDecision:
    new_state: str
    # "confirm_choice" | "clarify" | "give_up" | "reset_choice" | "reconfirm"
    # | "confirmed" | "wrong_number"
    reply_case: str
    chosen: Optional[str]
    new_attempts: int


def decide_turn(
    state: str, transcript: str, chosen: Optional[str], clarify_attempts: int,
) -> TurnDecision:
    """يقرر حتمياً — بلا أي نموذج — كيف تتطور مكالمة التأجيل بهذا الدور،
    بناءً على حالة الجلسة الحالية ورد الزبون. هذي الدالة **المصدر الوحيد**
    لمنطق المكالمة (انظر شرح الفلسفة بأعلى هذا القسم).

    رد فاضي (سكوت تام — transcribe يرجّع "" لو ماكو كلام مفهوم بالملف) يمر
    بنفس مسار "clarify"/"reconfirm" أدناه بلا معالجة خاصة: extract_postpone_
    choice/_is_yes/_is_no كلها ترجع سلباً لنص فاضٍ، فتنسحب تلقائياً لتوجيه
    "أعيدي السؤال" بدل ما تحتاج فرعاً منفصلاً — يطابق قسم "حالات خاصة"
    بـSABAH_SYSTEM_PROMPT (سكوت ورد غامض يُعامَلان بنفس التصعيد: محاولة
    وحدة ثانية، وإلا إغلاق بأدب)."""
    if _is_wrong_number(transcript):
        return TurnDecision("closed", "wrong_number", chosen, clarify_attempts)

    if state == "awaiting_confirmation":
        if _is_yes(transcript):
            return TurnDecision("closed", "confirmed", chosen, clarify_attempts)
        if _is_no(transcript):
            return TurnDecision("awaiting_choice", "reset_choice", None, 0)
        attempts = clarify_attempts + 1
        if attempts > session_store.MAX_CLARIFY_ATTEMPTS:
            return TurnDecision("closed", "give_up", chosen, attempts)
        return TurnDecision("awaiting_confirmation", "reconfirm", chosen, attempts)

    # state == "awaiting_choice" (الحالة الافتراضية عند بدء المكالمة)
    choice = extract_postpone_choice(transcript)
    if choice:
        return TurnDecision("awaiting_confirmation", "confirm_choice", choice, 0)
    attempts = clarify_attempts + 1
    if attempts > session_store.MAX_CLARIFY_ATTEMPTS:
        return TurnDecision("closed", "give_up", chosen, attempts)
    return TurnDecision("awaiting_choice", "clarify", chosen, attempts)


# توجيه داخلي حتمي لصباح حسب القرار (decide_turn) — هي تصوغه فقط بأسلوبها
# (انظر build_postpone_dialogue_prompt)، ورد احتياطي جاهز حرفياً بنفس
# الحالة لو الموديل غير جاهز (بلا GPU محلياً) — نفس فلسفة _FALLBACK_* أدناه.
_CASE_NOTES = {
    "confirm_choice": "الزبون اختار {option}. أكّدي اختياره بجملة قصيرة وانتظري تأكيده (نعم/لا).",
    "clarify": "ما وصل موعد واضح من الزبون (رد غامض، سكوت، أو موعد أبعد من أسبوعين). اسأليه بأدب يحدد موعد التسليم: اليوم، بكرة، بعد بكرة، أو أي يوم يناسبه خلال أسبوعين.",
    "give_up": "ما وضح اختيار الزبون رغم المحاولة. اعتذري بلطف، گولي إن فريقنا راح يعاود الاتصال، وانهي المكالمة.",
    "reset_choice": "الزبون رفض التأكيد وتراجع عن موعده. اسأليه من جديد يحدد الموعد اللي يناسبه.",
    "reconfirm": "رد الزبون على سؤال التأكيد غير واضح (احتمال سكوت أو كلام غير مفهوم). اسأليه بجملة أقصر: نعم لو لا بس.",
    "confirmed": "الزبون أكّد اختياره {option}. اشكريه بجملة قصيرة وانهي المكالمة بأدب.",
    "wrong_number": "تبين إن هذا مو الشخص المقصود بالطلب أو الرقم غلط. اعتذري بأدب جداً وانهي المكالمة فوراً بلا إصرار.",
}

_POSTPONE_FALLBACKS = {
    "confirm_choice": "تمام، خليها {option} إذن؟",
    "clarify": "عذراً، ما وضحت زين. تحب توصلك اليوم، بكرة، لو أي يوم ثاني يناسبك؟",
    "give_up": "ما مشكلة، فريقنا راح يعاود الاتصال بعدين. تصبح على خير.",
    "reset_choice": "تمام، شنو الموعد اللي يناسبك؟",
    "reconfirm": "بس تأكد لي: نعم لو لا؟",
    "confirmed": "تمام، خليناها {option}. مشكورين على وقتك، تصبح على خير.",
    "wrong_number": "عذراً على الإزعاج، يبدو صار خطأ بالرقم. تصبح على خير.",
}

_FALLBACK_POSTPONE_OPENING = (
    "هلا بيك، وياك صباح من خدمة العملاء. عدنا شحنتك بانتظار التسليم، "
    "حاب تستلمها اليوم، لو تفضّل موعد ثاني يناسبك؟"
)


async def _generate_postpone_opening(order: VoiceFollowupOrderRequest) -> str:
    if not llm_engine.ready:
        return _FALLBACK_POSTPONE_OPENING
    messages = build_postpone_opening_prompt(order)
    text = await llm_engine.generate_full(
        llm_engine.render_prompt(messages), max_tokens=140, temperature=0.0,
    )
    return text.strip() or _FALLBACK_POSTPONE_OPENING


async def _generate_postpone_reply(
    order: VoiceFollowupOrderRequest,
    history: List[dict],
    reply_case: str,
    chosen: Optional[str],
) -> str:
    # التسمية تحتاج عدد الأيام لا مفتاح الخيار — و"weekday_D" ما يحمل
    # عدداً بذاته، فنحسبه بتاريخ اليوم عبر postpone_days.
    label = option_label(chosen, postpone_days(chosen)) if chosen else ""
    directive = _CASE_NOTES[reply_case].format(option=label)
    fallback = _POSTPONE_FALLBACKS[reply_case].format(option=label)
    if not llm_engine.ready:
        return fallback
    messages = build_postpone_dialogue_prompt(order, history, directive)
    text = await llm_engine.generate_full(
        llm_engine.render_prompt(messages), max_tokens=90, temperature=0.0,
    )
    return text.strip() or fallback


@router.post("/ask")
async def voice_followup_ask(
    order: VoiceFollowupOrderRequest,
    api_key: str = Depends(require_voice_followup_api_key),
):
    """يستقبل تفاصيل طلب من باك اند السستم، يولّد سؤالاً صوتياً عن سبب
    حالته، ويرجع ملف صوت WAV مباشرة (audio/wav) — session_id ونص السؤال
    يصلان بالهيدرات حتى يبقى جسم الرد ملف صوت خام صالح للتشغيل مباشرة."""
    question_text = await _generate_question_text(order)
    audio_bytes = await run_in_threadpool(_synthesize_or_503, question_text)

    session_id = session_store.create(order)
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={_SESSION_HEADER: session_id, "X-Question-Text": _ascii_header(question_text)},
    )


@router.post("/respond")
async def voice_followup_respond(
    session_id: str,
    audio: UploadFile = File(...),
    api_key: str = Depends(require_voice_followup_api_key),
):
    """يستقبل session_id (من /ask) + ملف صوت رد الزبون، يحلّل السبب، يرسله
    لباك اند السستم، ويرجع صوت شكر جاهز للتشغيل مباشرة للزبون (audio/wav).
    التفاصيل (نص رد الزبون، ملخّص السبب، هل انرسل الـ query) تصل بالهيدرات."""
    order = session_store.pop(session_id)
    if order is None:
        raise HTTPException(404, "الجلسة غير موجودة أو انتهت صلاحيتها — استدعِ /ask من جديد.")

    audio_bytes = await audio.read()
    # transcribe() تزامنية وثقيلة (استدلال Whisper) — نفس مبرر order_intake/router.py:
    # تشغيلها مباشرة داخل async يجمّد الـ event loop لكل الطلبات المتزامنة.
    transcript = await run_in_threadpool(transcribe, audio_bytes)
    if transcript is None:
        raise HTTPException(503, "تحويل الصوت لنص غير متوفر محلياً (يحتاج transformers مثبَّتة).")
    if not transcript:
        raise HTTPException(422, "ما كدرنا نفهم أي كلام بالملف الصوتي.")

    reason_summary = await _analyze_customer_reply(order, transcript)

    query_sent = True
    try:
        await voice_followup_submitter.submit(order, reason_summary, transcript, api_key)
    except SystemBackendUnavailable:
        logger.exception(
            "فشل إرسال نتيجة المتابعة الصوتية لباك اند السستم (order_id=%s)", order.order_id,
        )
        query_sent = False

    thanks_audio = await run_in_threadpool(_synthesize_or_503, _FALLBACK_THANKS)
    return Response(
        content=thanks_audio,
        media_type="audio/wav",
        headers={
            "X-Reason-Summary": _ascii_header(reason_summary),
            "X-Customer-Transcript": _ascii_header(transcript),
            "X-Query-Sent": "true" if query_sent else "false",
        },
    )


@router.post("/postpone/start")
async def voice_postpone_start(
    order: VoiceFollowupOrderRequest,
    api_key: str = Depends(require_voice_followup_api_key),
):
    """يستقبل تفاصيل شحنة من باك اند السستم، يولّد جملة افتتاح مكالمة
    تأجيل التسليم (شخصية صباح)، ويرجع ملف صوت WAV مباشرة — session_id ونص
    الافتتاح يصلان بالهيدرات، نفس نمط /ask أعلاه."""
    opening_text = await _generate_postpone_opening(order)
    audio_bytes = await run_in_threadpool(_synthesize_or_503, opening_text)

    session_id = session_store.create_postpone(order, opening_text)
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={
            _SESSION_HEADER: session_id,
            "X-Reply-Text": _ascii_header(opening_text),
            "X-Call-Status": "continue",
        },
    )


@router.post("/postpone/respond")
async def voice_postpone_respond(
    session_id: str,
    audio: UploadFile = File(...),
    api_key: str = Depends(require_voice_followup_api_key),
):
    """يستقبل session_id (من /postpone/start) + رد الزبون الصوتي، يحسم
    الخطوة التالية حتمياً (decide_turn)، ويرجع صوت رد صباح.

    خلافاً لـ/respond أعلاه (دور واحد يُغلق الجلسة فوراً)، هذا المسار
    يُستدعى **بشكل متكرر بنفس session_id** طول مدة المكالمة —
    X-Call-Status يصير "ended" فقط لما تُغلق المكالمة فعلياً (تأكيد
    الزبون/رقم غلط/استسلام بعد محاولات)، وإلا يرجع "continue" ويبقى
    المتصل (باك اند السستم اللي يشغّل الصوت) يرسل دور رد جديد لنفس
    session_id."""
    session = session_store.get_postpone(session_id)
    if session is None:
        raise HTTPException(
            404, "جلسة مكالمة التأجيل غير موجودة أو انتهت صلاحيتها — استدعِ /postpone/start من جديد.",
        )

    audio_bytes = await audio.read()
    transcript = await run_in_threadpool(transcribe, audio_bytes)
    if transcript is None:
        raise HTTPException(503, "تحويل الصوت لنص غير متوفر محلياً (يحتاج transformers مثبَّتة).")
    # عمداً بلا HTTPException(422) على transcript الفاضي هنا (خلافاً لـ
    # /respond أعلاه) — سكوت الزبون بمكالمة تأجيل حالة عادية متوقَّعة
    # (انظر SABAH_SYSTEM_PROMPT قسم "حالات خاصة")، تُعامَل كرد غامض عادي
    # عبر decide_turn (تعيد السؤال مرة، ثم تقفل بأدب)، لا كخطأ HTTP.

    decision = decide_turn(session.state, transcript, session.chosen, session.clarify_attempts)
    session.history.append({"role": "user", "content": transcript})
    session.state = decision.new_state
    session.chosen = decision.chosen
    session.clarify_attempts = decision.new_attempts

    reply_text = await _generate_postpone_reply(
        session.order, session.history, decision.reply_case, decision.chosen,
    )
    session.history.append({"role": "assistant", "content": reply_text})

    call_status = "continue"
    postpone_saved = ""
    if session.state == "closed":
        call_status = "ended"
        if decision.reply_case == "confirmed" and decision.chosen:
            new_date = resolve_postpone_date(decision.chosen)
            try:
                await voice_postpone_submitter.submit(session.order, new_date, decision.chosen, api_key)
                postpone_saved = "true"
            except SystemBackendUnavailable:
                logger.exception(
                    "فشل إرسال قرار تأجيل التسليم لباك اند السستم (order_id=%s)",
                    session.order.order_id,
                )
                postpone_saved = "false"
        session_store.close_postpone(session_id)

    audio_reply = await run_in_threadpool(_synthesize_or_503, reply_text)
    return Response(
        content=audio_reply,
        media_type="audio/wav",
        headers={
            "X-Reply-Text": _ascii_header(reply_text),
            "X-Call-Status": call_status,
            "X-Chosen-Option": decision.chosen or "",
            "X-Customer-Transcript": _ascii_header(transcript),
            "X-Postpone-Saved": postpone_saved,
        },
    )
