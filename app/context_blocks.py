# -*- coding: utf-8 -*-
"""صياغة نتائج RAG (لهجة/منتجات) كمقاطع نصية تُضاف لأي system prompt.

مشتركة بين الميزات (sales، support، order_intake) بدل تكرارها بكل ميزة على حدة.
"""

from typing import List


def words_context_block(rag_words: List[dict]) -> str:
    if not rag_words:
        return ""
    lines = []
    for r in rag_words:
        if r.get("word"):
            lines.append(f"- {r['word']}: {r['meaning']}")
        else:
            lines.append(f"- {r['text']}")
    return "\n\nمعلومات مرجعية عن اللهجة العراقية (استخدمها إذا كانت مفيدة):\n" + "\n".join(lines)


def products_context_block(rag_products: List[dict]) -> str:
    if not rag_products:
        return "\n\nمنتجات متوفرة: لا يوجد أي منتج مطابق حالياً."
    lines = [
        f"- {p['name']} | السعر: {p['price']} {p.get('currency', '')} | "
        f"المخزون: {p.get('stock', 'غير محدد')} | {p.get('description', '')}"
        for p in rag_products
    ]
    return "\n\nمنتجات متوفرة (استخدم هذه الأسماء والأسعار فقط):\n" + "\n".join(lines)
