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
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from app.auth import require_voice_followup_api_key
from app.engine import llm_engine
from app.features.order_intake.transcribe import transcribe
from app.features.voice_followup import session_store, tts
from app.features.voice_followup.gateway import voice_followup_submitter
from app.features.voice_followup.prompts import build_analyze_prompt, build_ask_prompt
from app.features.voice_followup.schema import VoiceFollowupOrderRequest
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
