# -*- coding: utf-8 -*-
"""جلسات مكالمة "راجع" — تربط POST /voice_return/start بأدوار
POST /voice_return/respond المتتالية.

خلافاً لجلسات voice_followup (دور واحد: /ask يفتح، /respond يقرأ ويُغلق
فوراً)، مكالمة "راجع" تمتد لعدة أدوار إجبارياً: استئذان بدقيقة ← تأكيد
الهوية ← سؤال السبب ← عرض حل ← تلخيص وتأكيد نهائي. لذلك الحالة والتاريخ
يتراكمان بين استدعاء وآخر لنفس session_id لين تنتهي المكالمة فعلياً.

in-memory بالذاكرة (تُمسح عند إعادة تشغيل الخادم، تصلح فقط مع --workers 1)
— نفس قيد app/sessions.py بالضبط ولنفس السبب.
"""

import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from app.features.voice_return.schema import VoiceReturnOrderRequest

Message = Dict[str, str]

# مهلة الجلسة — نصف ساعة تكفي أي مكالمة حقيقية بفارق كبير، بلا ما تبقي
# جلسات معلّقة للأبد بالذاكرة إذا انقطع الاتصال بنص المكالمة.
_SESSION_TTL_SECONDS = 30 * 60

# محاولات التوضيح القصوى بكل مرحلة على حدة قبل ما صباح تستسلم وتقفل بأدب.
# رقم صغير عمداً: مكالمة حقيقية، وإعادة السؤال أكثر من مرتين تضجر الزبون —
# نفس القيمة والمبرر بـvoice_followup.
MAX_CLARIFY_ATTEMPTS = 2


# الشرح: حالة مكالمة "راجع" الكاملة. الحقول الأربعة الأولى هي اللي تقود
# قرار الراوتر بكل دور (decide_turn)، والباقي للتوثيق والإرسال النهائي.
#
# `stage` هو قلب الجلسة: مرحلة المكالمة الحالية. القيم المسموحة موثَّقة
# بالتعليق تحته مباشرة — أي قيمة خارجها تعني خطأ برمجي، مو حالة زبون.
#
# `reason_key` ينحفظ بعد تصنيف سبب الرجوع مرة وحدة، ويبقى محفوظاً لباقي
# المكالمة: الحل المعروض والقرار النهائي كلاهما يعتمدان عليه، فلو ضاع
# بعد التصنيف نضطر نعيد سؤال الزبون عن سببه — وهذا بالضبط اللي يمنعه
# الدليل ("لا تكرر الإقناع أكثر من مرة").
#
# `pending_decision` = القرار اللي راح ينحسم لو الزبون گال "نعم" بمرحلة
# التأكيد النهائي. نخزنه **قبل** ما نسأله، حتى يكون التلخيص اللي نسمعه
# للزبون هو نفسه القرار المُرسَل لباك اند السستم بالضبط — بلا إعادة حساب
# ممكن تطلع نتيجة مختلفة.
@dataclass
class ReturnCallSession:
    order: VoiceReturnOrderRequest
    history: List[Message] = field(default_factory=list)
    # "awaiting_permission" | "awaiting_identity" | "awaiting_authorization"
    # | "awaiting_reason" | "awaiting_solution" | "awaiting_final_confirm" | "closed"
    stage: str = "awaiting_permission"
    clarify_attempts: int = 0
    reason_key: Optional[str] = None
    pending_decision: Optional[str] = None
    customer_reason_text: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)


_sessions: Dict[str, "ReturnCallSession"] = {}


# الشرح: يفتح جلسة جديدة وتاريخها يبدأ بجملة افتتاح صباح. ليش نحفظ جملة
# الافتتاح بالتاريخ من الآن؟ لأن الدور الجاي يبنى على تاريخ المحادثة
# (build_dialogue_prompt)، ولو ضاعت الافتتاحية راح النموذج يشوف رد الزبون
# ("تفضل") بلا سياق أي سؤال، فيرد رداً غير مترابط.
def create(order: VoiceReturnOrderRequest, opening_text: str) -> str:
    session_id = str(uuid.uuid4())
    _sessions[session_id] = ReturnCallSession(
        order=order, history=[{"role": "assistant", "content": opening_text}],
    )
    return session_id


# الشرح: يرجع الجلسة إن وُجدت وما انتهت صلاحيتها، وإلا يحذفها ويرجع None.
# القراءة هنا **لا تُغلق** الجلسة (بعكس pop بـvoice_followup) — تبقى
# مفتوحة لأدوار لاحقة لين تُغلَق صراحة عبر close().
def get(session_id: str) -> Optional[ReturnCallSession]:
    session = _sessions.get(session_id)
    if session is None:
        return None
    if time.monotonic() - session.created_at > _SESSION_TTL_SECONDS:
        _sessions.pop(session_id, None)
        return None
    return session


# الشرح: يحذف الجلسة نهائياً ويرجعها — يُستدعى فقط بعد ما تنتهي المكالمة
# فعلياً (stage == "closed") حتى لا تبقى بالذاكرة بلا داعٍ.
def close(session_id: str) -> Optional[ReturnCallSession]:
    return _sessions.pop(session_id, None)
