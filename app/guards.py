# -*- coding: utf-8 -*-
"""حارس أرقام اختياري — يتأكد أن أي رقم مالي برد الموديل موجود حرفياً بنص
مرجعي (كتالوج/سياق RAG) بدل أن يكون مُختلَقاً. غير مربوط بأي راوتر تلقائياً؛
استدعِه يدوياً (log-only، بعد إرسال الرد للعميل — لا يضيف زمن استجابة) في أي
ميزة تحتاج هذا الضمان، مثل:

    from app.guards import check_numbers
    bad = check_numbers(answer, products_context_block(rag_products))
    if bad:
        logger.warning("أرقام مشبوهة برد الموديل: %s", bad)
"""

import re
from typing import List

_NUMBER_RE = re.compile(r"\d[\d,\.]*")


def check_numbers(reply: str, reference_text: str) -> List[str]:
    """يرجع قائمة الأرقام المالية بـ `reply` غير الموجودة حرفياً بـ
    `reference_text`. يتجاهل أرقاماً قصيرة بدون فواصل/عشرية (≤ رقمين) لأنها
    غالباً عدد قطع أو سنين ضمان، مو سعراً."""
    allowed = set(_NUMBER_RE.findall(reference_text))
    bad = []
    for num in _NUMBER_RE.findall(reply):
        clean = num.rstrip(".,")
        if clean in allowed:
            continue
        if "," not in clean and "." not in clean and len(clean) <= 2:
            continue
        bad.append(clean)
    return bad
