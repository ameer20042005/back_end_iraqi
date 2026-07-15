# -*- coding: utf-8 -*-
"""استخراج طلب من نص وحيد (بدل محادثة) — نص مكتوب مباشرة، أو ناتج تفريغ صوت،
أو (لاحقاً) وصف صورة. يعيد استخدام نفس مخطط ونص استخراج JSON الخاص بميزة
المبيعات بدل تكراره."""

from typing import Dict, List

from app.context_blocks import words_context_block
from app.features.sales.prompts import ORDER_EXTRACTION_SYSTEM_PROMPT

Message = Dict[str, str]


def build_order_intake_prompt(raw_text: str, rag_words: List[dict]) -> List[Message]:
    system_content = ORDER_EXTRACTION_SYSTEM_PROMPT + words_context_block(rag_words)
    return [
        {"role": "system", "content": system_content},
        # أمر التحويل الصريح بنهاية رسالة المستخدم (نفس أسلوب
        # build_order_extraction_prompt بميزة المبيعات) — بدونه الموديل
        # المدرَّب على ردود مبيعات باللهجة يميل يرد كبائع بدل إخراج JSON.
        {"role": "user", "content": f"نص طلب الزبون:\n{raw_text}\n\nحوّل النص أعلاه إلى JSON حسب المخطط المطلوب الآن — أخرج JSON فقط."},
    ]
