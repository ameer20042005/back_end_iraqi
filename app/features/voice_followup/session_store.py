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
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.features.voice_followup.schema import VoiceFollowupOrderRequest

Message = Dict[str, str]

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


# ---------------------------------------------------------------------------
# جلسات مكالمة "صباح" (تأجيل التسليم) — انظر prompts.py::SABAH_SYSTEM_PROMPT
# ---------------------------------------------------------------------------
#
# خلافاً للجلسات أعلاه (دور واحد: /ask يفتح، /respond يقرأ ويُغلق فوراً عبر
# pop)، مكالمة صباح قد تمتد لعدة أدوار: عرض الخيارات → توضيح إذا الرد غامض
# أو بخيار غير مسموح → تأكيد الاختيار قبل الإغلاق. لذلك الحالة (state/
# chosen/clarify_attempts) والتاريخ (history) يتراكمان بين استدعاء وآخر
# لنفس session_id لين تنتهي المكالمة فعلياً (state == "closed") — تخزين
# منفصل بقاموس ثانٍ (_postpone_sessions) حتى لا يتعارض مع جلسات JENI أعلاه
# اللي تستخدم شكل جلسة مختلف تماماً (Tuple بسيط بدل حالة متعددة الحقول).

# محاولات التوضيح القصوى قبل ما صباح تستسلم وتقفل المكالمة بأدب — بكل من
# مرحلة اختيار الموعد ومرحلة التأكيد على حدة (انظر router.py::decide_turn).
# رقم صغير عمداً: مكالمة حقيقية، إطالة التوضيح أكثر من مرتين تضجر الزبون.
MAX_CLARIFY_ATTEMPTS = 2


@dataclass
class PostponeSession:
    order: VoiceFollowupOrderRequest
    history: List[Message] = field(default_factory=list)
    # "awaiting_choice" | "awaiting_confirmation" | "closed"
    state: str = "awaiting_choice"
    chosen: Optional[str] = None  # "today" | "plus_1" | "plus_2"
    clarify_attempts: int = 0
    created_at: float = field(default_factory=time.monotonic)


_postpone_sessions: Dict[str, PostponeSession] = {}


def create_postpone(order: VoiceFollowupOrderRequest, opening_text: str) -> str:
    """يفتح جلسة مكالمة تأجيل جديدة، بتاريخ يبدأ بجملة افتتاح صباح، ويرجع
    session_id."""
    session_id = str(uuid.uuid4())
    _postpone_sessions[session_id] = PostponeSession(
        order=order, history=[{"role": "assistant", "content": opening_text}],
    )
    return session_id


def get_postpone(session_id: str) -> Optional[PostponeSession]:
    """يرجع جلسة التأجيل إن وُجدت وما انتهت صلاحيتها، وإلا يحذفها ويرجع
    None. خلافاً لـpop أعلاه، القراءة هنا لا تُغلق الجلسة — تبقى مفتوحة
    لأدوار لاحقة لين تُغلَق صراحة عبر close_postpone."""
    session = _postpone_sessions.get(session_id)
    if session is None:
        return None
    if time.monotonic() - session.created_at > _SESSION_TTL_SECONDS:
        _postpone_sessions.pop(session_id, None)
        return None
    return session


def close_postpone(session_id: str) -> Optional[PostponeSession]:
    """يحذف جلسة التأجيل نهائياً ويرجعها — يُستدعى فقط بعد ما تنتهي
    المكالمة فعلياً (state == "closed") حتى لا تبقى بالذاكرة بلا داعٍ."""
    return _postpone_sessions.pop(session_id, None)
