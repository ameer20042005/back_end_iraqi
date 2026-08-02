# -*- coding: utf-8 -*-
"""جلسات مسار المتابعة الصوتية — تربط POST /ask بـ POST /respond اللاحق.

مستقل عن app/sessions.py (تاريخ محادثة نصي متعدد الأدوار) لأن هنا نحتاج
تخزين كائن VoiceFollowupOrderRequest كاملاً لدور واحد فقط: /ask يفتح الجلسة
بمعطيات الطلب، /respond يقرأها مرة وحدة ثم يغلقها — لا تراكم تاريخ ولا
نافذة أدوار.

in-memory بالذاكرة (تُمسح عند إعادة تشغيل الخادم، تصلح فقط مع --workers 1)
— نفس قيد app/sessions.py بالضبط ولنفس السبب."""

import time
import uuid
from typing import Dict, Optional

from app.features.voice_followup.schema import VoiceFollowupOrderRequest

# مهلة انتظار رد الزبون قبل اعتبار الجلسة منتهية الصلاحية — نصف ساعة تكفي
# أي تأخر معقول بين تشغيل السؤال الصوتي للزبون واستلام رده، بلا ما تبقي
# جلسات معلّقة للأبد بالذاكرة.
_SESSION_TTL_SECONDS = 30 * 60

_sessions: Dict[str, "tuple[VoiceFollowupOrderRequest, float]"] = {}


def create(order: VoiceFollowupOrderRequest) -> str:
    """يفتح جلسة جديدة لهذا الطلب ويرجع session_id."""
    session_id = str(uuid.uuid4())
    _sessions[session_id] = (order, time.monotonic())
    return session_id


def pop(session_id: str) -> Optional[VoiceFollowupOrderRequest]:
    """يرجع معطيات الطلب المحفوظة بهذه الجلسة ويحذفها (استخدام مرة واحدة —
    رد الزبون بيُغلق دورة /ask→/respond). يرجع None إذا الجلسة غير موجودة
    أو منتهية الصلاحية."""
    entry = _sessions.pop(session_id, None)
    if entry is None:
        return None
    order, created_at = entry
    if time.monotonic() - created_at > _SESSION_TTL_SECONDS:
        return None
    return order
