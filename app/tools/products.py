# -*- coding: utf-8 -*-
"""أداة استعلام منتجات — متاحة لوكيل المبيعات عبر app/tool_loop.py.

النموذج نفسه يقرر متى يحتاج معلومة منتج ويطلبها صراحةً بـ tool_call؛ الأداة
هنا استعلام لحظي حقيقي على باك اند السستم (app/products.py) — بلا أي تخزين
أو فهرسة محلية، والنتيجة تُعالَج وتُرجَع للنموذج بلا احتفاظ بها هنا (عدا
كاش نتائج بحدود الجلسة، انظر _cache_key أدناه).

الفلاتر (category/in_stock_only) مبنية على سكيما جيني ستورز الحقيقية —
catalog.categories وcatalog.stock_info — انظر
assets/JENNI_STORES_SCHEMA_FOR_AI_QUERY_BUILDER.md. هذا الباك اند لا يعرف
شكل قيمها ولا يطابقها محلياً؛ يمررها كما هي لباك اند السستم الذي يملك
الكتالوج الحقيقي ويحدد معناها."""

import json
from typing import List

from app import sessions
from app.products import product_repository


def _cache_key(query: str, top_k: int, category: str, in_stock_only: bool) -> str:
    """مفتاح كاش حتمي لنفس تركيبة (استعلام + فلاتر) — استدعاءان بنفس
    المعاملات يطابقان نفس المفتاح بغض النظر عن ترتيب args بجسم tool_call."""
    return json.dumps(
        {"q": query, "top_k": top_k, "category": category, "in_stock": in_stock_only},
        ensure_ascii=False, sort_keys=True,
    )


async def search_products_tool(args: dict, api_key: str, session_id: str = "") -> dict:
    """دالة أداة متوافقة مع app.tool_loop.ToolFunc — تُربط بـ api_key/session_id
    الخاصين بطلب العميل الحالي عبر functools.partial وقت التسجيل بـ
    run_with_tools (انظر app/features/sales/router.py)، فما يمران بـ args
    التي يرسلها النموذج.

    args:
      - query: نص البحث (اسم منتج، وصف، فئة...) — إلزامي.
      - top_k: عدد النتائج (افتراضي 5).
      - category: اسم فئة (catalog.categories) لتضييق البحث — اختياري.
      - in_stock_only: true لعرض المتوفر بالمخزون فقط (catalog.stock_info) —
        اختياري، افتراضياً false (كل الكتالوج بغض النظر عن الكمية).
    """
    query = args.get("query")
    if not query:
        return {"error": "لازم تحدد query للبحث عن المنتج."}
    top_k = int(args.get("top_k", 5))
    category = str(args.get("category") or "").strip()
    in_stock_only = bool(args.get("in_stock_only", False))

    cache_key = _cache_key(str(query), top_k, category, in_stock_only)
    if session_id:
        cached = sessions.cached_product_search(session_id, cache_key)
        if cached is not None:
            return {"results": cached} if cached else {"results": [], "message": "ماكو منتج مطابق بالكتالوج."}

    results: List[dict] = await product_repository.search(
        str(query), api_key, top_k=top_k,
        category=category or None, in_stock_only=in_stock_only,
    )
    if session_id:
        sessions.cache_product_search(session_id, cache_key, results)
    if not results:
        return {"results": [], "message": "ماكو منتج مطابق بالكتالوج."}
    return {"results": results}
