# -*- coding: utf-8 -*-
"""برومت استخراج طلب من رسالة عراقية خام (نص/صوت/صورة) بمسار /orders/create.

الـ system prompt الكامل يعيش بملف plane.md بجذر المستودع (المصدر الوحيد —
عدّله هناك)، ويُحقن معه مرجع جغرافي من قاعدة بيانات شركة التوصيل
(app/rag/locations.py) حتى لا يخطئ الموديل بالمحافظة (city).
"""

import os
from typing import Dict, List

from app.context_blocks import locations_context_block, words_context_block
from app.rag import all_state_names

Message = Dict[str, str]

_PLANE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "plane.md"
)

# السطر الختامي بـ plane.md مصمَّم ليسبق نص الزبون مباشرة — ننقله لرسالة
# المستخدم حتى تُحقن كتل RAG (المواقع/اللهجة) قبله لا بعده.
_FINAL_INSTRUCTION = "الآن استخرج البيانات من النص التالي وأرجع JSON فقط:"

with open(_PLANE_PATH, encoding="utf-8") as _f:
    _SYSTEM_BASE = _f.read().strip()
if _SYSTEM_BASE.endswith(_FINAL_INSTRUCTION):
    _SYSTEM_BASE = _SYSTEM_BASE[: -len(_FINAL_INSTRUCTION)].rstrip()


def build_order_intake_prompt(
    raw_text: str,
    rag_words: List[dict],
    rag_locations: List[dict],
) -> List[Message]:
    system_content = (
        _SYSTEM_BASE
        + locations_context_block(rag_locations, all_state_names())
        + words_context_block(rag_words)
    )
    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": f"{_FINAL_INSTRUCTION}\n{raw_text}"},
    ]
