# -*- coding: utf-8 -*-
"""بدء مكالمة: غرفة LiveKit + dispatch صريح للـ agent + توكن لتطبيق سطح المكتب.

الـ agent نفسه (agent.py) هو اللي يتصل بالزبون عبر SIP لو مضبوط، حتى يبقى
تسلسل "اتصل ← انتظر الرد ← ابدأ الكلام" بمكان واحد.
"""

import re
import uuid
from datetime import timedelta

from livekit import api

from app.config import settings
from app.features.voice_followup.schema import AgentCallMetadata, CallRequest, CallResponse

OPERATOR_IDENTITY_PREFIX = "operator"
# الغرفة تُحذف تلقائياً إذا ما دخلها أحد خلال هذه المدة (مكالمة ما انربطت).
_EMPTY_TIMEOUT_SECONDS = 120
_UNSAFE_ROOM_CHARS = re.compile(r"[^A-Za-z0-9_-]+")


class LiveKitNotConfigured(RuntimeError):
    pass


def configured() -> bool:
    return bool(settings.livekit_url and settings.livekit_api_key and settings.livekit_api_secret)


def _operator_token(room: str) -> str:
    return (
        api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
        .with_identity(f"{OPERATOR_IDENTITY_PREFIX}-{uuid.uuid4().hex[:8]}")
        .with_name("operator")
        .with_ttl(timedelta(hours=1))
        .with_grants(api.VideoGrants(room_join=True, room=room))
        .to_jwt()
    )


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
    if not configured():
        raise LiveKitNotConfigured("LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET غير مضبوطة")

    room = room_name(request)
    metadata = agent_metadata(request)
    async with api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret,
    ) as lkapi:
        await lkapi.room.create_room(api.CreateRoomRequest(
            name=room, empty_timeout=_EMPTY_TIMEOUT_SECONDS,
        ))
        dispatch = await lkapi.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name=settings.livekit_agent_name,
            room=room,
            metadata=metadata.model_dump_json(),
        ))

    sip = bool(settings.livekit_sip_trunk_id and metadata.phone)
    return CallResponse(
        room=room,
        livekit_url=settings.livekit_url,
        operator_token=_operator_token(room),
        dispatch_id=dispatch.id,
        dial_mode="sip" if sip else "bridge",
    )
