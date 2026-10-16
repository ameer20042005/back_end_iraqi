# -*- coding: utf-8 -*-
"""بدء مكالمة: غرفة LiveKit + dispatch صريح للـ agent + توكن لتطبيق سطح المكتب.

الـ agent نفسه (agent.py) هو اللي يتصل بالزبون عبر SIP لو مضبوط، حتى يبقى
تسلسل "اتصل ← انتظر الرد ← ابدأ الكلام" بمكان واحد.
"""

import uuid
from datetime import timedelta

from livekit import api

from app.config import settings
from app.features.voice_followup.schema import CallRequest, CallResponse

OPERATOR_IDENTITY_PREFIX = "operator"
# الغرفة تُحذف تلقائياً إذا ما دخلها أحد خلال هذه المدة (مكالمة ما انربطت).
_EMPTY_TIMEOUT_SECONDS = 120


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


async def start_call(request: CallRequest) -> CallResponse:
    if not configured():
        raise LiveKitNotConfigured("LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET غير مضبوطة")

    room = f"call-{request.call_type}-{request.order.order_id}-{uuid.uuid4().hex[:6]}"
    async with api.LiveKitAPI(
        settings.livekit_url, settings.livekit_api_key, settings.livekit_api_secret,
    ) as lkapi:
        await lkapi.room.create_room(api.CreateRoomRequest(
            name=room, empty_timeout=_EMPTY_TIMEOUT_SECONDS,
        ))
        dispatch = await lkapi.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
            agent_name=settings.livekit_agent_name,
            room=room,
            metadata=request.model_dump_json(),
        ))

    sip = bool(settings.livekit_sip_trunk_id and request.order.customer_phone)
    return CallResponse(
        room=room,
        livekit_url=settings.livekit_url,
        operator_token=_operator_token(room),
        dispatch_id=dispatch.id,
        dial_mode="sip" if sip else "bridge",
    )
