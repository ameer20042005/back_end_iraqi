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


def locations_context_block(rag_locations: List[dict], state_names: List[str]) -> str:
    """مرجع المواقع من قاعدة بيانات شركة التوصيل (app/rag/locations.py) —
    يُحقن ببرومت استخراج الطلب: قيم city المسموحة هي أسماء states.xlsx
    الرسمية حصراً، وdistrict يُكتب بالاسم الرسمي من districts.xlsx عند
    وروده بالمطابقات."""
    block = (
        "\n\nقيم city المسموحة حصراً — الأسماء الرسمية للمحافظات بنظام شركة التوصيل"
        " (اكتب الاسم حرفياً كما هو هنا):\n"
        + "، ".join(state_names)
    )
    if rag_locations:
        lines = []
        for r in rag_locations:
            if r["district"]:
                states = "/".join(r["candidates"])
                lines.append(f"- المنطقة «{r['district']}» تتبع محافظة: {states}")
            else:
                lines.append(f"- «{r['state_name']}» محافظة")
        block += (
            "\n\nمرجع جغرافي مؤكد من قاعدة بيانات شركة التوصيل — أسماء وردت بالنص:\n"
            + "\n".join(lines)
            + "\nإذا ذُكرت منطقة من هذا المرجع بالنص فاكتب district باسمها المذكور"
            " أعلاه حرفياً، وcity بمحافظتها المذكورة أعلاه."
        )
    return block


def products_context_block(rag_products: List[dict]) -> str:
    if not rag_products:
        return "\n\nمنتجات متوفرة: لا يوجد أي منتج مطابق حالياً."
    lines = [
        f"- {p['name']} | السعر: {p['price']} {p.get('currency', '')} | "
        f"المخزون: {p.get('stock', 'غير محدد')} | {p.get('description', '')}"
        for p in rag_products
    ]
    return "\n\nمنتجات متوفرة (استخدم هذه الأسماء والأسعار فقط):\n" + "\n".join(lines)
