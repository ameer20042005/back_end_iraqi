# -*- coding: utf-8 -*-
"""صياغة نتائج RAG (لهجة/منتجات) كمقاطع نصية تُضاف لأي system prompt.

مشتركة بين الميزات (sales، support، order_intake) بدل تكرارها بكل ميزة على حدة.
"""

import json
import logging
from typing import List

logger = logging.getLogger(__name__)


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


def cap_for_model(items: List[dict], max_items: int, label: str) -> List[dict]:
    """يقصّ `items` لأول `max_items` فقط عند التسليم الفعلي للموديل (حقن
    بالبرومبت، أو رد أداة بأول استدعاء) — الكاش الكامل وراءه (app/sessions.py)
    يبقى بلا مساس؛ هذا القصّ لحظي وقت البناء فقط، حماية لميزانية التوكِن
    (settings.max_injected_records، انظر app/config.py). يسجّل تحذيراً لو
    انقصّ فعلياً — شفافية تشغيلية بدل ابتار صامت لتاريخ المحادثة أو فشل
    توليد لو كبر الكتالوج/دفتر الطلبات الحقيقي فوق المتوقَّع."""
    if len(items) <= max_items:
        return items
    logger.warning(
        "%s: %s عنصر يفوق سقف الحقن max_injected_records=%s — تم القصّ لأول %s.",
        label, len(items), max_items, max_items,
    )
    return items[:max_items]


def catalog_context_block(products: List[dict]) -> str:
    """كتالوج المنتجات **الكامل** (محمَّل مرة وحدة لهذي الجلسة عبر
    search_products_tool، انظر app/sessions.py::cache_catalog) — يُحقن بكل
    رسالة مبيعات لاحقة بنفس الجلسة (app/features/sales/prompts.py::
    build_sales_prompt) حتى يدوّر الموديل بالكتالوج كاملاً بدل استدعاء
    أداة جديد لكل منتج يُسأل عنه.

    JSON سطر لكل منتج — نفس شكل `[نتيجة الأداة search_products]` تماماً
    (انظر app/tool_loop.py) حتى تبقى قواعد SALES_SYSTEM_PROMPT ("من نتيجة
    search_products الحرفية فقط") صالحة بلا تفريق بين مصدرَي الكتالوج."""
    if not products:
        return ""
    lines = [json.dumps(p, ensure_ascii=False) for p in products]
    return (
        "\n\nكتالوج المنتجات الكامل (حُمِّل مرة وحدة هذي الجلسة — استخدمه "
        "حرفياً لأي سؤال منتج، بلا حاجة تستدعي search_products ثانية بهذي "
        "المحادثة إلا لو ما لگيت فيه جواب):\n" + "\n".join(lines)
    )


def orders_context_block(orders: List[dict]) -> str:
    """دفتر الطلبات **الكامل** (محمَّل مرة وحدة لهذي الجلسة، انظر
    app/sessions.py::cache_orders وapp/features/support/router.py::
    _list_all_cached) — يُحقن بكل رسالة دعم لاحقة (مسار "الموديل+الأداة"
    فقط؛ التتبع الحتمي برقم طلب/هاتف صريح لا يمر من هنا) حتى يدوّر الموديل
    بالدفتر كاملاً (بضمنه البحث باسم الزبون) بدل استدعاء get_order_status
    جديد لكل سؤال."""
    if not orders:
        return ""
    lines = [json.dumps(o, ensure_ascii=False) for o in orders]
    return (
        "\n\nدفتر الطلبات الكامل (حُمِّل مرة وحدة هذي الجلسة — استخدمه "
        "حرفياً للبحث بالاسم أو الهاتف أو الحالة، بلا حاجة تستدعي "
        "get_order_status ثانية إلا لو ما لگيت فيه جواب):\n" + "\n".join(lines)
    )


def products_context_block(rag_products: List[dict]) -> str:
    if not rag_products:
        return "\n\nمنتجات متوفرة: لا يوجد أي منتج مطابق حالياً."
    lines = [
        f"- {p['name']} | السعر: {p['price']} {p.get('currency', '')} | "
        f"المخزون: {p.get('stock', 'غير محدد')} | {p.get('description', '')}"
        for p in rag_products
    ]
    return "\n\nمنتجات متوفرة (استخدم هذه الأسماء والأسعار فقط):\n" + "\n".join(lines)
