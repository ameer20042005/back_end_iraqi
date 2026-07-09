# -*- coding: utf-8 -*-
"""ذاكرة محادثة بسيطة بالذاكرة (in-memory) — لكل جلسة تاريخ محدود من الأدوار.

ملاحظة: تُمسح عند إعادة تشغيل الخادم، وتصلح فقط مع --workers 1 (كما في
start.sh) لأنها غير مشتركة بين عدة عمليات.
"""

from collections import defaultdict
from typing import Dict, List

_MAX_TURNS = 12  # آخر N رسالة (مستخدم + مساعد) تُرسل كسياق

_sessions: Dict[str, List[Dict[str, str]]] = defaultdict(list)


def get(session_id: str) -> List[Dict[str, str]]:
    return _sessions.get(session_id, [])


def append(session_id: str, role: str, content: str) -> None:
    history = _sessions[session_id]
    history.append({"role": role, "content": content})
    if len(history) > _MAX_TURNS:
        del history[: len(history) - _MAX_TURNS]
