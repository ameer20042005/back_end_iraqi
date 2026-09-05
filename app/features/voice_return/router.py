# -*- coding: utf-8 -*-
"""مسار مكالمة "راجع": POST /voice_return/start و POST /voice_return/respond.

المسار كامل:
1) باك اند السستم يرسل تفاصيل شحنة راجعة لـ/start.
2) نولّد جملة افتتاح صباح (تعريف + استئذان بدقيقة)، نحوّلها لصوت
   (F5-TTS — نعيد استخدام voice_followup/tts.py نفسه)، ونرجعها مع
   session_id.
3) باك اند السستم يشغّل الصوت، يسجّل رد الزبون، ويرسله لـ/respond مع نفس
   session_id — **بشكل متكرر** طول المكالمة.
4) بكل دور: نحوّل الرد لنص (Whisper)، نحسم الخطوة التالية حتمياً
   (decide_turn)، ونرجع صوت رد صباح. لما تنتهي المكالمة نرسل القرار
   النهائي لباك اند السستم ونغلق الجلسة.

فلسفة الفصل — نفس مبدأ app/features/support/router.py: **كل** قرار
(هل وافق؟ هل هو المستلم؟ شنو القرار النهائي؟) يُحسب هنا بمطابقة نصية
حتمية على رد الزبون الحقيقي، والنموذج يُستدعى بعدها ليصوغ القرار
المحسوم بس. الاستثناء الوحيد تصنيف سبب الرجوع من كلام حر — استخراج لا
قرار (انظر prompts.py).
"""

import logging
import re
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.auth import require_voice_followup_api_key
from app.engine import llm_engine
from app.features.order_intake.transcribe import transcribe
from app.features.voice_followup import tts
from app.features.voice_return import session_store
from app.features.voice_return.gateway import voice_return_submitter
from app.features.voice_return.prompts import (
    RETURN_REASON_LABELS,
    build_dialogue_prompt,
    build_opening_prompt,
    build_reason_classify_prompt,
)
from app.features.voice_return.schema import VoiceReturnOrderRequest
from app.system_backend import SystemBackendUnavailable
from app.text_norm import normalize

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice_return", tags=["voice_return"])

_SESSION_HEADER = "X-Session-Id"


# ---------------------------------------------------------------------------
# قوائم المطابقة النصية — القوائم مكتوبة بصيغتها **المطبَّعة** (بعد
# normalize): بلا همزات ولا تاء مربوطة، وإلا ما تطابق أبداً.
# ---------------------------------------------------------------------------

_YES_WORDS = ("نعم", "ايوه", "ايه", "اي", "تمام", "زين", "موافق", "صح", "اوكي", "اكيد", "تفضل")
_NO_WORDS = ("لا", "كلا", "ماريد", "ما اريد", "مااريد", "ابدا", "مو راضي", "رافض")

# رقم غلط — أولوية قصوى بأي مرحلة: تُقفل المكالمة فوراً بلا محاولة متابعة.
_WRONG_NUMBER_HINTS = ("مو طلبي", "رقم غلط", "غلط الرقم", "منو تريد", "ماعندي طلب", "خطا الرقم", "غلط رقم", "مو الي")

# "مو أني المستلم" — يختلف عن الرقم الغلط: الشخص موجود ويعرف المستلم
# غالباً، فنسأله إذا مخوّل بدل ما نقفل فوراً.
_NOT_RECIPIENT_HINTS = ("مو المستلم", "مو انا المستلم", "اخوه", "زوجته", "قريبه", "مو صاحب الطلب")


# الشرح: مطابقة بحدود كلمة بدل substring. ليش ضرورية؟ لأن "لا" كـsubstring
# تنطابق داخل "لازم" و"بلاش" و"والله" — فرد إيجابي مثل "والله زين" ينقرأ
# رفضاً. حدود الكلمة تمنع هذا كلياً.
def _contains_word(normalized: str, words: "tuple[str, ...]") -> bool:
    return any(re.search(rf"\b{re.escape(w)}\b", normalized) for w in words)


# الشرح: أغلفة صغيرة حول _contains_word. مفصولة بأسماء واضحة لأن
# decide_turn أدناه يقرأ كجملة إنكليزية مفهومة بدونها ما ينقرأ.
def _is_yes(message: str) -> bool:
    return _contains_word(normalize(message), _YES_WORDS)


def _is_no(message: str) -> bool:
    return _contains_word(normalize(message), _NO_WORDS)


# الشرح: هذولا اثنان يستخدمان `in` (substring) مو حدود كلمة — عمداً، لأنها
# **عبارات** مركّبة مو كلمات مفردة ("مو طلبي"، "غلط الرقم")، واحتمال
# مطابقتها الكاذبة داخل كلمة أطول شبه معدوم.
def _is_wrong_number(message: str) -> bool:
    normalized = normalize(message)
    return any(h in normalized for h in _WRONG_NUMBER_HINTS)


def _is_not_recipient(message: str) -> bool:
    normalized = normalize(message)
    return any(h in normalized for h in _NOT_RECIPIENT_HINTS)


# ---------------------------------------------------------------------------
# القرارات النهائية وخريطة تحويلها لإجراء/حالة
# ---------------------------------------------------------------------------

# الشرح: خريطة القرار ← (الإجراء، الحالة الجديدة). منقولة حرفياً من
# CallDecisionProcessorImpl بخدمة الـ AI Call — القسم الخاص بحالة "راجع"
# ونتائجها الممكنة فقط.
#
# المبدأ الحاكم (من الدليل): **لا نثق بالإجراء اللي يقترحه النموذج** —
# نحسب الخريطة هنا بالخادم. عندنا الالتزام أقوى أصلاً لأن النموذج ما
# يقترح قراراً من الأساس؛ decide_turn هو اللي يحدده.
#
# None بمكان الحالة تعني "لا تغيّر حالة الشحنة" — الفرق مهم: NO_ACTION_
# NEEDED تسجّل ملاحظة بس (رقم غلط/غير مخوّل)، ما تحرّك الشحنة بأي اتجاه.
DECISION_MAP = {
    "CONFIRMED_RETURN": ("RETURN_APPROVED", "APPROVAL_RETURN"),
    "CUSTOMER_WILL_RECEIVE": ("TREATED", "TRY_AGAIN"),
    "CUSTOMER_READY_NOW": ("TREATED", "TRY_AGAIN"),
    "CONFIRMED_POSTPONED": ("APPROVAL_POSTPONED", "APPROVAL_POSTPONED"),
    "NO_ACTION_NEEDED": ("NO_ACTION_NEEDED", None),
}


# الشرح: يحوّل (سبب الرجوع + هل قبل الزبون الحل المعروض؟) إلى القرار
# النهائي. هذي الدالة هي **جوهر منطق العمل** كله بسطور معدودة، وكل سطر
# فيها يطابق فرعاً بقسم "إذا الحالة راجع" بالدليل:
#
# - ما طلبها + قبل (أي: أحد طلبها باسمه أو غلط متجر) ← راح يستلمها.
# - ما طلبها + رفض ← راجع نهائي.
# - ماكو وقت + قبل التأجيل ← تأجيل مؤكَّد.
# - مشكلة مندوب + قبل إعادة الإرسال ← راح يستلمها.
# - متردد + قبل ← جاهز يستلم الآن.
# - رافض + "نعم أثبتها راجع" ← راجع نهائي.
# - رافض + "لا" ← تناقض (رفض إثبات الرجوع رغم رفضه الاستلام)، نرجع نسأله
#   عن سببه من جديد بدل ما نخمّن — يطابق قاعدة الدليل: عند التضارب اطلب
#   تأكيداً، لا تحسم.
def _resolve_decision(reason_key: str, accepted: bool) -> Optional[str]:
    if reason_key == "refused":
        return "CONFIRMED_RETURN" if accepted else None
    if not accepted:
        return "CONFIRMED_RETURN"
    return {
        "no_order": "CUSTOMER_WILL_RECEIVE",
        "no_time": "CONFIRMED_POSTPONED",
        "agent_issue": "CUSTOMER_WILL_RECEIVE",
        "hesitant": "CUSTOMER_READY_NOW",
    }[reason_key]


# ---------------------------------------------------------------------------
# آلة حالات المكالمة
# ---------------------------------------------------------------------------

# الشرح: نتيجة دور واحد. نرجّعها ككائن بدل tuple لأن الحقول أربعة ومعانيها
# غير متشابهة — tuple بأربع خانات يصير غير مقروء بمكان الاستدعاء.
@dataclass
class TurnDecision:
    new_stage: str
    reply_case: str
    new_attempts: int
    pending_decision: Optional[str] = None


# الشرح: **المصدر الوحيد** لمنطق المكالمة. حتمية بالكامل: نفس المدخلات
# تعطي نفس المخرجات دائماً، بلا أي استدعاء نموذج — فتنختبر بلا GPU.
#
# `reason_key` يجي مُصنَّفاً مسبقاً من المستدعي (لأن تصنيفه يحتاج نموذج)،
# فتبقى هذي الدالة نفسها خالية من أي استدعاء خارجي.
#
# رد فاضي (سكوت تام — transcribe يرجّع "" لو ماكو كلام مفهوم) يمر بمسار
# "clarify" تلقائياً بلا فرع خاص: كل دوال الفحص ترجع سلباً لنص فاضٍ. هذا
# يطابق قسم "حالات خاصة" بالبرومت (سكوت ورد غامض يُعامَلان بنفس التصعيد).
def decide_turn(
    stage: str,
    transcript: str,
    reason_key: Optional[str],
    clarify_attempts: int,
) -> TurnDecision:
    # الرقم الغلط يُفحص أولاً وبمعزل عن المرحلة: لو طلع الشخص غير
    # المقصود، ما عاد لأي منطق تالٍ معنى — نقفل فوراً بلا إصرار.
    if _is_wrong_number(transcript):
        return TurnDecision("closed", "wrong_number", clarify_attempts, "NO_ACTION_NEEDED")

    if stage == "awaiting_permission":
        if _is_yes(transcript):
            return TurnDecision("awaiting_identity", "permission_granted", 0)
        if _is_no(transcript):
            # مستعجل: ما نقفل ولا نلحّ — ننتقل لسؤال الحسم القصير
            # مباشرة (يطابق "إذا مستعجل: اسأله سؤال حسم قصير" بالدليل).
            return TurnDecision("awaiting_reason", "permission_busy", 0)
        return _clarify_or_give_up(stage, clarify_attempts)

    if stage == "awaiting_identity":
        if _is_not_recipient(transcript):
            return TurnDecision("awaiting_authorization", "identity_denied", 0)
        if _is_yes(transcript):
            return TurnDecision("awaiting_reason", "identity_confirmed", 0)
        if _is_no(transcript):
            return TurnDecision("awaiting_authorization", "identity_denied", 0)
        return _clarify_or_give_up(stage, clarify_attempts)

    if stage == "awaiting_authorization":
        if _is_yes(transcript):
            return TurnDecision("awaiting_reason", "authorized", 0)
        if _is_no(transcript):
            return TurnDecision("closed", "not_authorized", clarify_attempts, "NO_ACTION_NEEDED")
        return _clarify_or_give_up(stage, clarify_attempts)

    if stage == "awaiting_reason":
        # reason_key يجي None لو النموذج صنّف "unclear" أو ما كان جاهزاً.
        if reason_key in RETURN_REASON_LABELS:
            return TurnDecision("awaiting_solution", "reason_captured", 0)
        return _clarify_or_give_up(stage, clarify_attempts)

    if stage == "awaiting_solution":
        accepted = _is_yes(transcript)
        declined = _is_no(transcript)
        if not accepted and not declined:
            return _clarify_or_give_up(stage, clarify_attempts)
        decision = _resolve_decision(reason_key or "hesitant", accepted)
        if decision is None:
            # تضارب (رفض الاستلام ورفض إثبات الرجوع) — نرجع لسؤال السبب.
            return TurnDecision("awaiting_reason", "reset_reason", 0)
        return TurnDecision("awaiting_final_confirm", "summarize", 0, decision)

    if stage == "awaiting_final_confirm":
        if _is_yes(transcript):
            return TurnDecision("closed", "confirmed", clarify_attempts)
        if _is_no(transcript):
            # تراجع عن التلخيص — نرجعه لسؤال السبب من جديد، ونمسح القرار
            # المعلّق حتى لا ينرسل قرار ما وافق عليه.
            return TurnDecision("awaiting_reason", "reset_reason", 0)
        return _clarify_or_give_up(stage, clarify_attempts)

    # مرحلة غير معروفة = خطأ برمجي لا حالة زبون. نقفل بأمان بدل ما نرمي
    # استثناء بنص مكالمة حقيقية مع زبون.
    return TurnDecision("closed", "give_up", clarify_attempts, "NO_ACTION_NEEDED")


# الشرح: التصعيد الموحَّد للرد الغامض/السكوت — مكرر بكل مرحلة، فاستخرجناه
# بدالة وحدة. بعد تجاوز الحد نقفل بأدب مع NO_ACTION_NEEDED: ما وصلنا لقرار،
# فممنوع نحرّك حالة الشحنة بأي اتجاه.
def _clarify_or_give_up(stage: str, clarify_attempts: int) -> TurnDecision:
    attempts = clarify_attempts + 1
    if attempts > session_store.MAX_CLARIFY_ATTEMPTS:
        return TurnDecision("closed", "give_up", attempts, "NO_ACTION_NEEDED")
    return TurnDecision(stage, "clarify", attempts)


# ---------------------------------------------------------------------------
# التوجيهات الداخلية والردود الاحتياطية
# ---------------------------------------------------------------------------

# الشرح: التوجيه الداخلي الحتمي لصباح حسب القرار — هي تصوغه بأسلوبها فقط
# (build_dialogue_prompt)، ما تقرر منطقه. `{solution}` يُملأ من
# _SOLUTION_NOTES أدناه حسب سبب الرجوع المصنَّف.
_CASE_NOTES = {
    "permission_granted": "الزبون سمح لك بدقيقة. اذكري الشحنة بسطر واحد (اسم المتجر والمبلغ) وحالتها إنها راجعة، ثم اسأليه: حضرتك المستلم؟",
    "permission_busy": "الزبون مستعجل. بجملة وحدة قصيرة جداً اسأليه مباشرة عن سبب رجوع الشحنة، بلا مقدمات.",
    "identity_confirmed": "الزبون أكّد إنه المستلم. اسأليه سؤال واحد: شنو سبب رجوع الشحنة؟",
    "identity_denied": "الزبون گال إنه مو المستلم. اسأليه سؤال واحد بس: هل هو مخوّل يأكد القرار عن المستلم، أو يگدر يوصلك إله؟",
    "authorized": "الشخص مخوّل يقرر. اسأليه سؤال واحد: شنو سبب رجوع الشحنة؟",
    "not_authorized": "الشخص غير مخوّل يقرر عن المستلم. اشكريه بأدب وأنهي المكالمة فوراً بلا إصرار.",
    "wrong_number": "تبين إن هذا مو الشخص المقصود أو الرقم غلط. اعتذري بأدب جداً وأنهي المكالمة فوراً بلا إصرار.",
    "reason_captured": "فهمتي سبب الزبون. گولي 'تمام فهمتك' ثم اعرضي عليه هذا الحل الواحد بس: {solution}",
    "reset_reason": "الزبون ما وافق على الحل ولا على التلخيص. اسأليه من جديد بجملة قصيرة: شنو سبب رجوع الشحنة بالضبط؟",
    "summarize": "لخّصي القرار بجملة وحدة قصيرة واسأليه نعم أو لا بس، حتى تتأكدين: {summary}",
    "confirmed": "الزبون أكّد القرار. اختمي بجملة وحدة بس: تمام، شكراً إلك، مع السلامة.",
    "clarify": "رد الزبون غير واضح أو ماكو رد (احتمال سكوت). كرري آخر سؤال بصيغة أقصر بكثير مرة وحدة بس.",
    "give_up": "ما وضح رد الزبون رغم المحاولة. اعتذري بلطف، گولي إن فريقنا راح يعاود الاتصال، وأنهي المكالمة.",
}


# الشرح: نص الحل المعروض لكل سبب — منقول حرفياً من فروع "بعد السبب: اقترح
# حل واحد مناسب" بالدليل. مفصول عن _CASE_NOTES لأنه يتغيّر حسب reason_key
# مو حسب reply_case.
_SOLUTION_NOTES = {
    "no_order": "اسأليه إذا ممكن أحد طلبها باسمه أو صارت من المتجر بالغلط، وإذا لا فراح نثبتها راجع نهائي.",
    "no_time": "اعرضي عليه تأجيل التوصيل لوقت يناسبه.",
    "agent_issue": "اعرضي عليه إنك راح تعالجينها وتعيدين إرسالها إله بتنسيق أوضح.",
    "hesitant": "اعرضي عليه إنك ترتبين إعادتها إله والتوصيل يصير اليوم.",
    "refused": "اسأليه للتأكيد إذا تثبتينها راجع نهائي.",
}


# الشرح: تلخيص القرار المعروض على الزبون قبل الحسم — نص كل قرار بصيغة
# يفهمها الزبون، مو بالرمز الإنكليزي. لازم يطابق القرار المُرسَل فعلاً
# لباك اند السستم، وإلا نكون سمّعناه شي وسجّلنا شي ثاني.
_DECISION_SUMMARIES = {
    "CONFIRMED_RETURN": "الشحنة راح تنثبت راجعة نهائياً",
    "CUSTOMER_WILL_RECEIVE": "راح نعيد إرسال الشحنة إله ويستلمها",
    "CUSTOMER_READY_NOW": "راح نرتب التوصيل إله اليوم",
    "CONFIRMED_POSTPONED": "راح نأجل التوصيل للوقت اللي يناسبه",
}


# الشرح: ردود احتياطية حتمية لو النموذج النصي غير جاهز (محلياً بلا GPU) —
# نفس فلسفة _FALLBACK_* بـvoice_followup: نص جاهز مقبول بدل ما نمنع الميزة
# كاملة. مكتوبة بنفس نبرة صباح حتى ما يحس الزبون بفرق لو انشغّلت.
_FALLBACKS = {
    "permission_granted": "شحنتك راجعة عدنا. حضرتك المستلم؟",
    "permission_busy": "بجملة وحدة، شنو سبب رجوع الشحنة؟",
    "identity_confirmed": "تمام. ممكن أعرف شنو سبب رجوع الشحنة؟",
    "identity_denied": "فهمت. حضرتك مخوّل تأكد القرار عن المستلم؟",
    "authorized": "تمام. شنو سبب رجوع الشحنة؟",
    "not_authorized": "ما مشكلة، مشكورين على وقتك، مع السلامة.",
    "wrong_number": "عذراً على الإزعاج، يبدو صار خطأ بالرقم. مع السلامة.",
    "reason_captured": "تمام فهمتك.",
    "reset_reason": "عذراً، شنو سبب رجوع الشحنة بالضبط؟",
    "summarize": "حتى أتأكد، {summary}، صح؟",
    "confirmed": "تمام، شكراً إلك، مع السلامة.",
    "clarify": "عذراً، ما سمعتك زين، ممكن تعيد؟",
    "give_up": "ما مشكلة، فريقنا راح يعاود الاتصال بعدين. مع السلامة.",
}

_FALLBACK_OPENING = "هلا بيك، وياك صباح من خدمة العملاء، إذا تسمح دقيقة؟"


# الشرح: يرمّز النص العربي للهيدر. هيدرات HTTP لازم Latin-1، ونص عربي
# فيها يكسر الاستجابة كلياً — percent-encoding يوصل النص كاملاً بلا فقدان،
# والمستدعي يفكه بـurllib.parse.unquote. نفس الحل بـvoice_followup.
def _ascii_header(value: str) -> str:
    return quote(value)


# الشرح: يحوّل النص لصوت أو يرمي 503. مفصولة بدالة لأنها تنستدعى بأربع
# أماكن، وكلها لازم ترجع نفس رسالة الخطأ العربية.
def _synthesize_or_503(text: str) -> bytes:
    audio = tts.synthesize(text)
    if audio is None:
        raise HTTPException(503, "تحويل النص لصوت غير متوفر محلياً (يحتاج f5-tts مثبَّتة).")
    return audio


# الشرح: يصنّف سبب الرجوع من كلام الزبون الحر. يرجع None لو النموذج غير
# جاهز أو صنّف "unclear" — و None تقود decide_turn لإعادة السؤال، مو لقرار
# مخترَع. temperature=0.0 لأن التصنيف مهمة حتمية ما تحتمل تنويعاً.
# async def _classify_reason(order: VoiceReturnOrderRequest, transcript: str) -> Optional[str]:
#     if not llm_engine.ready:
#         return None
#     messages = build_reason_classify_prompt(order, transcript)
#     raw = await llm_engine.generate_full(
#         llm_engine.render_prompt(messages), max_tokens=8, temperature=0.0,
#     )
#     key = raw.strip().strip('".').lower()
#     return key if key in RETURN_REASON_LABELS else None


# الشرح: يولّد نص رد صباح لهذا الدور. يبني التوجيه الداخلي أولاً (بملء
# {solution}/{summary})، ثم يمرره للنموذج — ولو النموذج غير جاهز يرجع الرد
# الاحتياطي المقابل لنفس الحالة، فيبقى منطق المكالمة شغالاً بلا GPU.
# async def _generate_reply(
#     order: VoiceReturnOrderRequest,
#     history: List[dict],
#     reply_case: str,
#     reason_key: Optional[str],
#     pending_decision: Optional[str],
# ) -> str:
#     solution = _SOLUTION_NOTES.get(reason_key or "", "")
#     summary = _DECISION_SUMMARIES.get(pending_decision or "", "")
#     directive = _CASE_NOTES[reply_case].format(solution=solution, summary=summary)
#     fallback = _FALLBACKS[reply_case].format(summary=summary)
#     if not llm_engine.ready:
#         return fallback
#     messages = build_dialogue_prompt(order, history, directive)
#     text = await llm_engine.generate_full(
#         llm_engine.render_prompt(messages), max_tokens=90, temperature=0.0,
#     )
#     return text.strip() or fallback


# ---------------------------------------------------------------------------
# نقاط النهاية
# ---------------------------------------------------------------------------

# الشرح: يبدأ المكالمة. يرجع **ملف صوت خام** بالجسم (audio/wav) حتى يگدر
# باك اند السستم يشغّله مباشرة بلا فك أي غلاف JSON — والبيانات الوصفية
# (session_id، نص الجملة، حالة المكالمة) تمشي بالهيدرات. نفس نمط
# /voice_followup/ask بالضبط.
@router.post("/start")
async def voice_return_start(
    order: VoiceReturnOrderRequest,
    api_key: str = Depends(require_voice_followup_api_key),
):
    opening_text = _FALLBACK_OPENING
    if llm_engine.ready:
        messages = build_opening_prompt(order)
        generated = await llm_engine.generate_full(
            llm_engine.render_prompt(messages), max_tokens=60, temperature=0.0,
        )
        opening_text = generated.strip() or _FALLBACK_OPENING

    audio_bytes = await run_in_threadpool(_synthesize_or_503, opening_text)
    session_id = session_store.create(order, opening_text)
    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={
            _SESSION_HEADER: session_id,
            "X-Reply-Text": _ascii_header(opening_text),
            "X-Call-Status": "continue",
        },
    )


# الشرح: دور واحد بالمكالمة. يُستدعى **متكرراً بنفس session_id** لين
# X-Call-Status يرجع "ended".
#
# ترتيب الخطوات هنا مقصود ولا يتغيّر:
# 1) نحوّل الصوت لنص.
# 2) نصنّف السبب **فقط** لو كنا بمرحلة سؤال السبب (نداء نموذج إضافي، ما
#    ننفذه بكل دور بلا داعٍ).
# 3) نحسم الدور حتمياً.
# 4) نحدّث الجلسة، ثم نولّد رد صباح.
# 5) لو انتهت المكالمة: نرسل القرار لباك اند السستم ونغلق الجلسة.
#
# لاحظ غياب HTTPException(422) على النص الفاضي (بعكس /voice_followup/
# respond): سكوت الزبون بمكالمة حقيقية حالة عادية متوقَّعة، يعالجها
# decide_turn كرد غامض (يعيد السؤال مرة، ثم يقفل بأدب) — لا كخطأ HTTP
# يقطع المكالمة على باك اند السستم.
@router.post("/respond")
async def voice_return_respond(
    session_id: str,
    audio: UploadFile = File(...),
    api_key: str = Depends(require_voice_followup_api_key),
):
    session = session_store.get(session_id)
    if session is None:
        raise HTTPException(
            404, "جلسة مكالمة الراجع غير موجودة أو انتهت صلاحيتها — استدعِ /voice_return/start من جديد.",
        )

    audio_bytes = await audio.read()
    # transcribe() تزامنية وثقيلة (استدلال Whisper) — تشغيلها مباشرة داخل
    # async يجمّد الـevent loop لكل الطلبات المتزامنة.
    transcript = await run_in_threadpool(transcribe, audio_bytes)
    if transcript is None:
        raise HTTPException(503, "تحويل الصوت لنص غير متوفر محلياً (يحتاج transformers مثبَّتة).")

    reason_key = session.reason_key
    if session.stage == "awaiting_reason":
        classified = await _classify_reason(session.order, transcript)
        if classified:
            reason_key = classified
            session.customer_reason_text = transcript

    decision = decide_turn(session.stage, transcript, reason_key, session.clarify_attempts)

    session.history.append({"role": "user", "content": transcript})
    session.stage = decision.new_stage
    session.clarify_attempts = decision.new_attempts
    session.reason_key = reason_key
    if decision.pending_decision:
        session.pending_decision = decision.pending_decision

    reply_text = await _generate_reply(
        session.order, session.history, decision.reply_case,
        reason_key, session.pending_decision,
    )
    session.history.append({"role": "assistant", "content": reply_text})

    call_status = "continue"
    result_sent = ""
    final_decision = ""
    if session.stage == "closed":
        call_status = "ended"
        # decision.pending_decision يغلب pending المحفوظ: قرارات الإغلاق
        # المبكر (رقم غلط/غير مخوّل) تجي بالقرار نفسه، أما "confirmed"
        # فتعتمد على القرار المخزَّن من مرحلة التلخيص.
        final_decision = decision.pending_decision or session.pending_decision or "NO_ACTION_NEEDED"
        action, new_status = DECISION_MAP[final_decision]
        try:
            await voice_return_submitter.submit(
                session.order, final_decision, action, new_status,
                session.reason_key, session.customer_reason_text, api_key,
            )
            result_sent = "true"
        except SystemBackendUnavailable:
            logger.exception(
                "فشل إرسال قرار مكالمة الراجع لباك اند السستم (order_id=%s)",
                session.order.order_id,
            )
            result_sent = "false"
        session_store.close(session_id)

    audio_reply = await run_in_threadpool(_synthesize_or_503, reply_text)
    return Response(
        content=audio_reply,
        media_type="audio/wav",
        headers={
            "X-Reply-Text": _ascii_header(reply_text),
            "X-Call-Status": call_status,
            "X-Call-Stage": session.stage,
            "X-Return-Reason": session.reason_key or "",
            "X-Decision": final_decision,
            "X-Customer-Transcript": _ascii_header(transcript),
            "X-Result-Sent": result_sent,
        },
    )
