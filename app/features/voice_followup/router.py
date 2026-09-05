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
    OPTION_LABELS,
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

# "يومين" تحتوي حرفياً "يوم" (أول ثلاثة أحرف)، فلازم نفحص خيار اليومين
# أولاً وإلا "بعد يومين" تُصنَّف غلط كـ"بعد يوم" — نفس تحذير extract_status
# بـsupport/router.py (نطابق الأطول أولاً). القوائم مكتوبة بصيغتها المطبَّعة
# (بعد normalize) — بلا همزات ولا تاء مربوطة.
_TWO_DAYS_WORDS = ("يومين", "بعد يومين", "بيومين")
_ONE_DAY_WORDS = ("بعد يوم", "غدا", "باچر", "بكره", "يوم واحد")
_TODAY_WORDS = ("اليوم", "هسه", "نفس اليوم", "هذا اليوم", "الحين")

_YES_WORDS = ("نعم", "ايوه", "ايه", "اي", "تمام", "زين", "موافق", "صح", "اوكي")
_NO_WORDS = ("لا", "كلا", "ماريد", "ما اريد", "تراجعت", "غيرها", "بديل")

# رقم غلط / شخص غير مقصود — أولوية قصوى بغض النظر عن حالة المكالمة (انظر
# decide_turn أدناه): تُقفل المكالمة فوراً بلا محاولة متابعة الموضوع.
_WRONG_NUMBER_HINTS = ("مو طلبي", "رقم غلط", "غلط الرقم", "منو تريد", "ماعندي طلب", "خطا الرقم", "غلط رقم")


def _contains_word(normalized: str, words: "tuple[str, ...]") -> bool:
    """مطابقة بحدود كلمة (\\b) — يمنع مطابقة كاذبة لكلمات قصيرة مثل "لا"
    داخل كلمة أطول ("لازم")، خلافاً لفحص substring بسيط."""
    return any(re.search(rf"\b{re.escape(w)}\b", normalized) for w in words)


def extract_postpone_choice(message: str) -> Optional[str]:
    """يستخرج خيار التأجيل («today»/«plus_1»/«plus_2») من رد الزبون
    المحوَّل من صوت لنص، أو None إذا ما طابق أي خيار من الثلاثة المسموحة
    (خارج النطاق، أو رد غامض/غير مفهوم)."""
    normalized = normalize(message)
    if any(w in normalized for w in _TWO_DAYS_WORDS):
        return "plus_2"
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


_OPTION_OFFSETS = {"today": 0, "plus_1": 1, "plus_2": 2}


def resolve_postpone_date(choice: str, today: Optional[date] = None) -> str:
    """يحسب تاريخ ISO الفعلي لخيار تأجيل مؤكَّد. `today` قابلة للتمرير
    للاختبار (تاريخ ثابت بدل تاريخ التشغيل الفعلي) — نفس نمط
    support/router.py::_resolve_relative_range."""
    today = today or date.today()
    return (today + timedelta(days=_OPTION_OFFSETS[choice])).isoformat()


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
    "clarify": "رد الزبون غير واضح أو خارج الخيارات الثلاثة (احتمال سكوت أو كلام غير مفهوم). ذكّريه بأدب إن الخيارات المتاحة بس: اليوم، بعد يوم، أو بعد يومين، واسأليه يختار وحدة منهم.",
    "give_up": "ما وضح اختيار الزبون رغم المحاولة. اعتذري بلطف، گولي إن فريقنا راح يعاود الاتصال، وانهي المكالمة.",
    "reset_choice": "الزبون رفض التأكيد وتراجع عن اختياره. اسأليه من جديد يختار وحدة من: اليوم، بعد يوم، أو بعد يومين.",
    "reconfirm": "رد الزبون على سؤال التأكيد غير واضح (احتمال سكوت أو كلام غير مفهوم). اسأليه بجملة أقصر: نعم لو لا بس.",
    "confirmed": "الزبون أكّد اختياره {option}. اشكريه بجملة قصيرة وانهي المكالمة بأدب.",
    "wrong_number": "تبين إن هذا مو الشخص المقصود بالطلب أو الرقم غلط. اعتذري بأدب جداً وانهي المكالمة فوراً بلا إصرار.",
}

_POSTPONE_FALLBACKS = {
    "confirm_choice": "تمام، خليها {option} إذن؟",
    "clarify": "عذراً، ما وضحت زين. الخيارات عدنا: اليوم، بعد يوم، أو بعد يومين — شنو تفضل؟",
    "give_up": "ما مشكلة، فريقنا راح يعاود الاتصال بعدين. تصبح على خير.",
    "reset_choice": "تمام، شنو تفضل: اليوم، بعد يوم، لو بعد يومين؟",
    "reconfirm": "بس تأكد لي: نعم لو لا؟",
    "confirmed": "تمام، خليناها {option}. مشكورين على وقتك، تصبح على خير.",
    "wrong_number": "عذراً على الإزعاج، يبدو صار خطأ بالرقم. تصبح على خير.",
}

_FALLBACK_POSTPONE_OPENING = (
    "هلا بيك، وياك صباح من خدمة العملاء. عدنا شحنتك بانتظار التسليم، "
    "حاب تستلمها اليوم، لو تفضّل نأجل يوم أو يومين؟"
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
    option_label = OPTION_LABELS.get(chosen or "", "")
    directive = _CASE_NOTES[reply_case].format(option=option_label)
    fallback = _POSTPONE_FALLBACKS[reply_case].format(option=option_label)
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
