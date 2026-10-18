# -*- coding: utf-8 -*-
"""Reserve a short-lived Pipecat WebSocket session for the WhatsApp bridge."""

import re
import uuid
import time


from app.features.voice_followup.schema import AgentCallMetadata, CallRequest, CallResponse

_UNSAFE_ROOM_CHARS = re.compile(r"[^A-Za-z0-9_-]+")
_pending = {}
_PENDING_TTL = 120
_MAX_PENDING = 64


def consume_stream_id(stream_id: str, request: CallRequest) -> str:
    _prune_pending()
    pending = _pending.get(stream_id)
    if pending is None or pending[2] != request:
        raise ValueError("stream_id invalid, expired, consumed or call does not match")
    del _pending[stream_id]
    return pending[1]


def _prune_pending() -> None:
    now = time.monotonic()
    for key, (expires, _, _) in list(_pending.items()):
        if expires <= now:
            del _pending[key]


def room_name(request: CallRequest) -> str:
    """اسم غرفة آمن (يطابق outcomes.ROOM_RE) ومميَّز بـ reference_id لو موجود."""
    ref = _UNSAFE_ROOM_CHARS.sub("", request.reference_id or "")[:40]
    return f"call-{ref}-{uuid.uuid4().hex[:8]}" if ref else f"call-{uuid.uuid4().hex[:12]}"


def _redact(text, secrets):
    if not text:
        return text
    for secret in secrets:
        text = text.replace(secret, "")
    return text


def agent_metadata(request: CallRequest) -> AgentCallMetadata:
    """metadata الـ agent: بلا اسم الزبون، ويُشال الاسم/الرقم لو انكتبا بالنصوص الحرة."""
    secrets = [s.strip() for s in (request.contact.name, request.contact.phone) if s and len(s.strip()) >= 2]
    return AgentCallMetadata(
        company_name=request.company_name,
        agent_name=request.agent_name or None,
        goal=_redact(request.goal, secrets),
        questions=[_redact(q, secrets) for q in request.questions],
        context=_redact(request.context, secrets) or None,
        reference_id=request.reference_id or None,
        language=request.language,
        phone=request.contact.phone or None,
    )


async def start_call(request: CallRequest) -> CallResponse:
    _prune_pending()
    if len(_pending) >= _MAX_PENDING:
        raise RuntimeError("Too many calls awaiting WebSocket connection")
    stream_id = uuid.uuid4().hex
    room = room_name(request)
    _pending[stream_id] = (time.monotonic() + _PENDING_TTL, room, request.model_copy(deep=True))
    return CallResponse(room=room, livekit_url="", operator_token="", dispatch_id="",
                        dial_mode="bridge", transport="pipecat", stream_path="/voice_followup/stream",
                        stream_id=stream_id, expires_in_seconds=_PENDING_TTL)
