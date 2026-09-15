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

import json
import logging
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
    option_label_ku,
    build_analyze_prompt,
    build_ask_prompt,
    build_postpone_dialogue_prompt,
    build_postpone_opening_prompt,
    build_turn_understanding_prompt,
)
from app.features.voice_followup.schema import VoiceFollowupOrderRequest
from app.lang import Lang, detect
from app.system_backend import SystemBackendUnavailable

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
# مكالمة "صباح" (تأجيل التسليم) — القرار حتمي، والفهم اللغوي بالنموذج
# ---------------------------------------------------------------------------
#
# **الخط الفاصل، بثلاث طبقات:**
#
#   ١. الفهم (نموذج): رد الزبون الحر — بأي لغة أو لهجة — يتحوّل لمفتاح من
#      POSTPONE_CHOICES عبر TURN_SCHEMA. مقيَّد بـguided_json، فالنموذج ما
#      يقدر يرجّع قيمة خارج الجدول أصلاً.
#   ٢. التثبّت (كود): parse_understanding ترفض أي شذوذ وتسقطه لـ"unclear"،
#      لأن ضمانة الطبقة ١ تجي من خدمة خارجية عبر الشبكة.
#   ٣. القرار والحساب (كود): decide_turn تقرر مسار المكالمة،
#      وresolve_postpone_date تحسب التاريخ. **ولا واحدة منهما تستدعي نموذجاً.**
#
# فما ينحفظ بباك اند السستم يبقى ناتج حساب حتمي على مفتاح من جدول مغلق.
#
# كانت الطبقة ١ مطابقة نصية على جداول كلمات ثابتة ("باجر"، "بكره"،
# "اسبوعين"، أسماء الأيام…). انحذفت لسببين: أي صيغة مو بالقائمة تُعتبر
# رداً غامضاً حتى لو معناها واضح، وكل لغة جديدة تحتاج نسخة كاملة من
# الجداول بتطبيع خاص بحروفها.

# الحد الأقصى للتأجيل. سقف ضروري لسببين: يمنع قيماً عبثية ("أجلها سنة")
# تصير التزاماً تشغيلياً ما ننفّذه، ويمنع خطأ Whisper برقم منطوق (يسمع
# "أربعين" بدل "أربعة") من تحويل مكالمة عادية لتأجيل مستحيل. أي رقم فوقه
# يُعامَل كخيار غير مسموح، فتعيد صباح السؤال بدل ما تثبّته.
MAX_POSTPONE_DAYS = 14

def _days_choice(days: int) -> Optional[str]:
    """يحوّل عدد أيام لمفتاح خيار، أو None لو تجاوز السقف أو كان سالباً.
    الصفر يصير "today" حتى يبقى مفتاح اليوم واحداً بكل المسارات."""
    if days < 0 or days > MAX_POSTPONE_DAYS:
        return None
    return "today" if days == 0 else f"plus_{days}"


# ---------------------------------------------------------------------------
# جدول الخيارات — المصدر الحتمي الوحيد للمواعيد
# ---------------------------------------------------------------------------
#
# **ليش مولَّد بالكود لا مكتوب يدوياً؟** لأنه مشتق من MAX_POSTPONE_DAYS.
# كتابته يدوياً يعني أن رفع السقف من 14 لـ21 يتطلب تذكّر تعديل قائمة ثانية
# بمكان ثاني — وأول مرة تُنسى، النموذج يصير يقدر يرجّع خياراً ما يقدر
# الخادم يحسبه. التوليد يخلي السقف رقماً واحداً يحكم المنظومة كلها.
#
# القيم هي **مفاتيح** لا تواريخ: "plus_3" لا "2026-09-17". التاريخ الفعلي
# يُحسب بالخادم عبر resolve_postpone_date وقت التثبيت فقط. هذا الفصل هو
# بيت القصيد — النموذج يختار من قائمة مغلقة، والحساب يبقى حتمياً بالكود.
def _all_postpone_choices() -> "tuple[str, ...]":
    """كل مفاتيح التأجيل المسموحة: اليوم + كل عدد أيام لين السقف + أيام
    الأسبوع السبعة (بترقيم date.weekday(): الاثنين=0 … الأحد=6)."""
    return (
        ("today",)
        + tuple(f"plus_{d}" for d in range(1, MAX_POSTPONE_DAYS + 1))
        + tuple(f"weekday_{d}" for d in range(7))
    )


POSTPONE_CHOICES = _all_postpone_choices()


# الشرح: مخطط الاستخراج المقيَّد (guided_json). vLLM يفرضه **فعلياً** وقت
# التوليد لا كتلميح بالبرومبت (انظر app/engine.py::_build_body) — يعني
# النموذج **ما يقدر فيزيائياً** يرجّع قيمة خارج هذي القوائم.
#
# وهذا بالضبط الجواب على الخطر اللي يخلق لما ننقل الفهم للنموذج: السقف
# (MAX_POSTPONE_DAYS) ما عاد يُفحص بشرط `if` ممكن ننساه — صار **مفروضاً
# بالمخطط نفسه**، لأن "plus_20" أصلاً مو من ضمن القيم المولَّدة أعلاه.
# النموذج يفهم اللغة، والحدود تبقى بالكود.
#
# intent مفصول عن choice عمداً: الزبون ممكن يگول "نعم" (تأكيد) أو "لا"
# (تراجع) أو "رقم غلط" — وكلها ليست خيار موعد. دمجهما بحقل واحد يخلط
# معنيين مختلفين ويجبر الخادم يخمّن أيهما قُصد.
TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": ["choice", "yes", "no", "wrong_number", "unclear"],
        },
        "choice": {
            "type": "string",
            # "none" قيمة صريحة لا null — تبسّط الفحص بجهة الخادم وتتجنب
            # اختلاف تعامل المولّدات المقيَّدة مع القيم الفارغة.
            "enum": list(POSTPONE_CHOICES) + ["none"],
        },
    },
    "required": ["intent", "choice"],
    "additionalProperties": False,
}


@dataclass
class Understanding:
    """ما فهمه النموذج من دور الزبون — بعد التثبّت بجهة الخادم."""
    intent: str
    choice: Optional[str]


# الشرح: التثبّت بجهة الخادم. **ليش نتثبّت رغم أن guided_json يضمن الشكل؟**
# لأن الضمانة تجي من خدمة خارجية (vLLM) عبر الشبكة: إصدار يتغيّر، إعداد
# يُنسى، مسار احتياطي يشتغل بلا تقييد — وأي واحدة منها تحوّل الضمانة
# لافتراض صامت. الكلفة سطور معدودة، والبديل تاريخ غلط ينحفظ بباك اند
# السستم. أي رد ما يمر بالفحص يُعامَل كـ"unclear"، فتعيد صباح السؤال
# بدل ما تثبّت شي مشكوك فيه — نفس سلوك الرد الغامض بالضبط.
def parse_understanding(raw: Optional[str]) -> Understanding:
    """يفكّك ويتثبّت من مخرَج النموذج. أي شذوذ يسقط لـ"unclear" بأمان."""
    if not raw:
        return Understanding("unclear", None)
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("مخرَج فهم الدور مو JSON صالح: %r", raw[:200])
        return Understanding("unclear", None)

    if not isinstance(data, dict):
        return Understanding("unclear", None)

    intent = data.get("intent")
    if intent not in ("choice", "yes", "no", "wrong_number", "unclear"):
        return Understanding("unclear", None)

    choice = data.get("choice")
    # الخيار يُقبل **فقط** مع intent="choice"، و**فقط** لو كان من الجدول
    # المولَّد أعلاه. رد يقول intent="yes" ويحمل choice يُتجاهل خياره —
    # ما نخلي النموذج يثبّت موعداً بدور مخصَّص للتأكيد.
    if intent == "choice" and choice in POSTPONE_CHOICES:
        return Understanding("choice", choice)
    if intent == "choice":
        logger.warning("خيار تأجيل خارج الجدول المسموح: %r", choice)
        return Understanding("unclear", None)
    return Understanding(intent, None)


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
    للاختبار (تاريخ ثابت بدل تاريخ التشغيل الفعلي)."""
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
    state: str, understanding: Understanding, chosen: Optional[str], clarify_attempts: int,
) -> TurnDecision:
    """يقرر **حتمياً** كيف تتطور مكالمة التأجيل بهذا الدور.

    ⚠️ **ما تغيّر وما بقي، بعد نقل الفهم للنموذج:**
      · تغيّر: مصدر الفهم. كانت تستقبل نص الزبون الخام وتطابقه بجداول
        كلمات (باجر/بكره/غدا…)، صارت تستقبل Understanding مُثبَّتة.
      · بقي: **كل منطق المكالمة**. عدّ المحاولات، متى تُقفل، متى ترجع
        لسؤال الموعد، أولوية الرقم الغلط — كلها هنا بالكود كما كانت،
        بلا أي نموذج. النموذج يفهم اللغة فقط؛ لا يقرر ولا يحسب تاريخاً.

    وهذا يحافظ على الوعد المكتوب بأعلى هذا القسم: ما ينحفظ بباك اند
    السستم يبقى ناتج حساب حتمي (resolve_postpone_date) على مفتاح من
    جدول مغلق — لا نصاً حراً من نموذج.

    رد فاضٍ (سكوت تام) يوصل كـintent="unclear" فينسحب لمسار إعادة السؤال
    بلا فرع خاص — يطابق قسم "حالات خاصة" بـSABAH_SYSTEM_PROMPT."""
    if understanding.intent == "wrong_number":
        return TurnDecision("closed", "wrong_number", chosen, clarify_attempts)

    if state == "awaiting_confirmation":
        if understanding.intent == "yes":
            return TurnDecision("closed", "confirmed", chosen, clarify_attempts)
        if understanding.intent == "no":
            return TurnDecision("awaiting_choice", "reset_choice", None, 0)
        attempts = clarify_attempts + 1
        if attempts > session_store.MAX_CLARIFY_ATTEMPTS:
            return TurnDecision("closed", "give_up", chosen, attempts)
        return TurnDecision("awaiting_confirmation", "reconfirm", chosen, attempts)

    # state == "awaiting_choice" (الحالة الافتراضية عند بدء المكالمة)
    if understanding.intent == "choice" and understanding.choice:
        return TurnDecision("awaiting_confirmation", "confirm_choice", understanding.choice, 0)
    attempts = clarify_attempts + 1
    if attempts > session_store.MAX_CLARIFY_ATTEMPTS:
        return TurnDecision("closed", "give_up", chosen, attempts)
    return TurnDecision("awaiting_choice", "clarify", chosen, attempts)


# توجيه داخلي حتمي لصباح حسب القرار (decide_turn) — هي تصوغه فقط بأسلوبها
# (انظر build_postpone_dialogue_prompt)، ورد احتياطي جاهز حرفياً بنفس
# الحالة لو الموديل غير جاهز (بلا GPU محلياً) — نفس فلسفة _FALLBACK_* أدناه.
_CASE_NOTES = {
    "confirm_choice": "الزبون اختار {option}. أكّدي اختياره بجملة قصيرة وانتظري تأكيده (نعم/لا).",
    "clarify": "ما وصل موعد واضح من الزبون (رد غامض، سكوت، أو موعد أبعد من أسبوعين). اسأليه بأدب يحدد موعد التسليم: اليوم، باجر، بعد باجر، أو أي يوم يناسبه خلال أسبوعين.",
    "give_up": "ما وضح اختيار الزبون رغم المحاولة. اعتذري بلطف، گولي إن فريقنا راح يعاود الاتصال، وانهي المكالمة.",
    "reset_choice": "الزبون رفض التأكيد وتراجع عن موعده. اسأليه من جديد يحدد الموعد اللي يناسبه.",
    "reconfirm": "رد الزبون على سؤال التأكيد غير واضح (احتمال سكوت أو كلام غير مفهوم). اسأليه بجملة أقصر: نعم لو لا بس.",
    "confirmed": "الزبون أكّد اختياره {option}. اشكريه بجملة قصيرة وانهي المكالمة بأدب.",
    "wrong_number": "تبين إن هذا مو الشخص المقصود بالطلب أو الرقم غلط. اعتذري بأدب جداً وانهي المكالمة فوراً بلا إصرار.",
}

_POSTPONE_FALLBACKS = {
    "confirm_choice": "تمام، خليها {option} إذن؟",
    "clarify": "عذراً، ما وضحت زين. تحب توصلك اليوم، باجر، لو أي يوم ثاني يناسبك؟",
    "give_up": "ما مشكلة، فريقنا راح يعاود الاتصال بعدين. تصبح على خير.",
    "reset_choice": "تمام، شنو الموعد اللي يناسبك؟",
    "reconfirm": "بس تأكد لي: نعم لو لا؟",
    "confirmed": "تمام، خليناها {option}. مشكورين على وقتك، تصبح على خير.",
    "wrong_number": "عذراً على الإزعاج، يبدو صار خطأ بالرقم. تصبح على خير.",
}

# نفس الردود، لكن للسوراني. هذا مهم تحديداً بمسار fallback: قاعدة البرومبت
# تضمن لغة الرد عندما النموذج جاهز، أما هنا فالخادم هو الذي يكتب النص بنفسه.
# نبقي القاموسين منفصلين بدلاً من ترجمة آلية حتى تكون العبارة المنطوقة طبيعية
# وتبقى خالية تماماً من العربية عند الزبون الكردي.
_POSTPONE_FALLBACKS_KU = {
    "confirm_choice": "باشە، بۆ {option} دایدەنین، ڕاستە؟",
    "clarify": "ببورە، کاتی گونجاوت ڕوون نەبوو. ئەمڕۆ، سبەینێ، یان کەی بۆت باشترە؟",
    "give_up": "کێشە نییە، تیمەکەمان دواتر پەیوەندیت پێوە دەکات. خوات لەگەڵ.",
    "reset_choice": "باشە، کاتێکی تر کەی بۆت گونجاوە؟",
    "reconfirm": "تکایە تەنها بەڵێ یان نەخێر بڵێ.",
    "confirmed": "باشە، بۆ {option} دایدەنین. سوپاس بۆ کاتت، خوات لەگەڵ.",
    "wrong_number": "ببورە بۆ ناڕەحەتییەکە، وایە ژمارەکە هەڵەیە. خوات لەگەڵ.",
}

_FALLBACK_POSTPONE_OPENING = (
    "هلا بيك، وياك صباح من خدمة العملاء. عدنا شحنتك بانتظار التسليم، "
    "حاب تستلمها اليوم، لو تفضّل موعد ثاني يناسبك؟"
)


def _postpone_option_label(chosen: Optional[str], language: Lang) -> str:
    """تسمية الموعد بنفس لغة الرد الاحتياطي، أو نص فارغ إذا ماكو موعد."""
    if not chosen:
        return ""
    days = postpone_days(chosen)
    if language is Lang.KU:
        return option_label_ku(chosen, days)
    return option_label(chosen, days)


def _postpone_fallback(reply_case: str, chosen: Optional[str], language: Lang) -> str:
    """يرجع الرد الحتمي بلغته، بما فيه تسمية الموعد المؤكَّد."""
    label = _postpone_option_label(chosen, language)
    fallbacks = _POSTPONE_FALLBACKS_KU if language is Lang.KU else _POSTPONE_FALLBACKS
    return fallbacks[reply_case].format(option=label)


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
    # آخر عنصر بالتاريخ هو رد الزبون الذي يُجاب عنه الآن. الكشف هنا يخص
    # fallback فقط؛ حين النموذج جاهز يرى الرد والتعليمة الموحدة بنفسه.
    language = detect(history[-1]["content"]) if history else Lang.AR
    # التسمية تحتاج عدد الأيام لا مفتاح الخيار — و"weekday_D" ما يحمل
    # عدداً بذاته، فنحسبه بتاريخ اليوم عبر postpone_days.
    label = _postpone_option_label(chosen, language)
    directive = _CASE_NOTES[reply_case].format(option=label)
    fallback = _postpone_fallback(reply_case, chosen, language)
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


async def _understand_turn(transcript: str, state: str) -> Understanding:
    """يحوّل رد الزبون الحر لفهم مُثبَّت عبر النموذج بمخطط مقيَّد.

    ⚠️ **بلا نموذج جاهز** (تشغيل محلي بلا GPU) يرجع "unclear" دائماً. هذا
    تراجع مقصود عن السلوك السابق: المطابقة النصية المحذوفة كانت تشتغل بلا
    نموذج، فمكالمة التأجيل كانت قابلة للاختبار محلياً كاملة. بعد نقل الفهم
    للنموذج صار مسار المكالمة يحتاجه فعلياً — والبديل (إبقاء الجداول
    كاحتياطي) يعيد نفس عبء الصيانة اللي انحذفت لأجله، وينتج سلوكاً مختلفاً
    بين المحلي والإنتاج وهو أسوأ من سلوك واحد واضح.

    temperature=0.0 لأن هذي مهمة استخراج لا صياغة — نريد نفس المخرَج لنفس
    الرد بكل مرة، حتى يبقى سلوك المكالمة قابلاً لإعادة الإنتاج عند التحقيق
    بأي شكوى زبون."""
    if not llm_engine.ready:
        logger.warning("النموذج غير جاهز — يُعامَل رد الزبون كغامض")
        return Understanding("unclear", None)

    messages = build_turn_understanding_prompt(transcript, state)
    raw = await llm_engine.generate_full(
        llm_engine.render_prompt(messages),
        max_tokens=64,
        temperature=0.0,
        guided_json=TURN_SCHEMA,
    )
    return parse_understanding(raw)


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

    understanding = await _understand_turn(transcript, session.state)
    decision = decide_turn(
        session.state, understanding, session.chosen, session.clarify_attempts,
    )
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
