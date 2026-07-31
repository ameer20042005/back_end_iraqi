# -*- coding: utf-8 -*-
"""أداة استعلام منتجات — متاحة لوكيل المبيعات عبر app/tool_loop.py.

النموذج نفسه يقرر متى يحتاج معلومة منتج ويطلبها صراحةً بـ tool_call؛ الأداة
هنا استعلام لحظي حقيقي على باك اند السستم (app/products.py) — بلا أي تخزين
أو فهرسة محلية، والنتيجة تُعالَج وتُرجَع للنموذج بلا احتفاظ بها هنا."""

from typing import List

from app.products import product_repository


async def search_products_tool(args: dict, api_key: str) -> dict:
    """دالة أداة متوافقة مع app.tool_loop.ToolFunc — تُربط بـ api_key الخاص
    بطلب العميل الحالي عبر functools.partial وقت التسجيل بـ run_with_tools
    (انظر app/features/sales/router.py)، فما يمر بـ args التي يرسلها النموذج.

    args:
      - query: نص البحث (اسم منتج، وصف، فئة...) — إلزامي.
      - top_k: عدد النتائج (افتراضي 5).
    """
    query = args.get("query")
    if not query:
        return {"error": "لازم تحدد query للبحث عن المنتج."}
    top_k = args.get("top_k", 5)
    results: List[dict] = await product_repository.search(str(query), api_key, top_k=int(top_k))
    if not results:
        return {"results": [], "message": "ماكو منتج مطابق بالكتالوج."}
    return {"results": results}
