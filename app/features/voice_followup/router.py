# -*- coding: utf-8 -*-
"""Pipecat calls: operator chat, session reservation, PCM WebSocket and outcomes."""

import logging

from fastapi import APIRouter, Depends, HTTPException, WebSocket
from fastapi.responses import StreamingResponse

from app.auth import require_voice_followup_api_key
from app.core.engine import llm_engine
from app.features.voice_followup import calls, outcomes, stream
from app.features.voice_followup.operator import OperatorChatRequest, completion_events, run_operator_turn
from app.features.voice_followup.schema import CallOutcome, CallRequest, CallResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/voice_followup", tags=["voice_followup"])


@router.post("/calls", response_model=CallResponse)
async def create_call(
    req: CallRequest,
    _api_key: str = Depends(require_voice_followup_api_key),
) -> CallResponse:
    """يبدأ مكالمة حية مع جهة اتصال وحدة. الجسم هو نفسه وسائط أداة start_call."""
    try:
        return await calls.start_call(req)
    except Exception as exc:
        logger.exception("فشل بدء مكالمة %s", req.reference_id or "-")
        raise HTTPException(502, "تعذّر تجهيز جلسة المكالمة؛ راجع سجل الخادم") from exc


@router.get("/calls/{room}/outcome", response_model=CallOutcome)
async def get_call_outcome(
    room: str,
    _api_key: str = Depends(require_voice_followup_api_key),
) -> CallOutcome:
    """نتيجة المكالمة لو انحفظت، وإلا status="pending"."""
    try:
        return outcomes.get_outcome(room)
    except outcomes.InvalidRoom as exc:
        raise HTTPException(422, "اسم الغرفة غير صالح") from exc


@router.post("/operator/chat/completions")
async def operator_chat(
    req: OperatorChatRequest,
    _api_key: str = Depends(require_voice_followup_api_key),
):
    """جولة توليد واحدة لمساعد الموظف؛ التطبيق ينفّذ الأدوات ويعيد نتائجها."""
    response = await run_operator_turn(llm_engine, req)
    if req.stream:
        return StreamingResponse(
            completion_events(response),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return response


@router.websocket("/stream")
async def call_stream(websocket: WebSocket) -> None:
    """مكالمة صوتية كاملة عبر WebSocket (المفتاح بهيدر X-API-Key أو ?api_key=)."""
    await stream.serve(websocket)
