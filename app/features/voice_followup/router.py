# -*- coding: utf-8 -*-
"""المكالمات الصوتية الحية عبر LiveKit — نقاط الـ API.

- POST /voice_followup/operator/chat/completions: مساعد الموظف النصي (عقد OpenAI).
  الموظف يكتب "اتصل بهؤلاء واسألهم كذا"، والنموذج يستعمل أدوات التطبيق ويرجع
  start_call لكل شخص.
- POST /voice_followup/calls: تنفيذ start_call — غرفة LiveKit + إرسال الـ agent
  (agent.py) + توكن لتطبيق سطح المكتب يراقب أو يربط بيه صوت المكالمة.
- GET /voice_followup/calls/{room}/outcome: التطبيق يسحب نتيجة المكالمة
  (pending لحد ما الـ agent يحفظها).

الحوار الصوتي نفسه ما يمر بهذي العملية: يعيش بالـ agent worker.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.auth import require_voice_followup_api_key
from app.core.engine import llm_engine
from app.features.voice_followup import calls, outcomes
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
    except calls.LiveKitNotConfigured as exc:
        raise HTTPException(503, f"المكالمات الصوتية غير مفعّلة: {exc}") from exc
    except Exception as exc:
        logger.exception("فشل بدء مكالمة %s", req.reference_id or "-")
        raise HTTPException(502, f"تعذّر بدء المكالمة عبر LiveKit: {exc}") from exc


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
