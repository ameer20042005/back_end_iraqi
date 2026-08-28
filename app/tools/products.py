# -*- coding: utf-8 -*-
"""أداة تحميل كتالوج المنتجات — متاحة لوكيل المبيعات عبر app/tool_loop.py.

**تصميم "حمّل مرة وحدة"** (بدل بحث ضيّق لكل منتج): أول استدعاء بأي جلسة
مبيعات يجيب الكتالوج **كاملاً** من باك اند السستم (app/products.py::list_all)
ويخزّنه بكاش الجلسة (app/sessions.py::cache_catalog) — استدعاءات لاحقة
بنفس الجلسة تلگى الكاش مباشرة بلا أي طلب HTTP جديد. الموديل نفسه يدوّر
بالكتالوج الكامل (محقوناً بكل رسالة لاحقة عبر
app/features/sales/prompts.py::build_sales_prompt، انظر
app/context_blocks.py::catalog_context_block) بدل ما يستدعي بحثاً جديداً
لكل صنف يُسأل عنه — هذا هو الغرض من الأداة، لا فهرسة/بحث محلي هنا.

الفلاتر (category/in_stock_only) مبنية على سكيما جيني ستورز الحقيقية —
catalog.categories وcatalog.stock_info — انظر
assets/JENNI_STORES_SCHEMA_FOR_AI_QUERY_BUILDER.md. تُطبَّق محلياً على
الكتالوج المخزَّن (لا فلترة بجهة باك اند السستم بعد الآن) لتقصير رد **هذا
الدور تحديداً** فقط — الكتالوج المحقون بالجولات القادمة يبقى كاملاً بلا
مساس."""

from typing import List

from app import sessions
from app.config import settings
from app.context_blocks import cap_for_model
from app.products import product_repository


def _filter_catalog(catalog: List[dict], category: str, in_stock_only: bool) -> List[dict]:
    """تضييق محلي اختياري على الكتالوج المخزَّن — لا يمس الكاش نفسه، فقط
    رد هذا الدور. `query` **لا** يضيّق النتيجة (يُقرأ ويُهمَل عمداً — قرار
    تصميم: الموديل هو من يفلتر من الكتالوج الكامل المحقون، لا الأداة)."""
    filtered = catalog
    if category:
        filtered = [p for p in filtered if (p.get("category") or "") == category]
    if in_stock_only:
        filtered = [p for p in filtered if p.get("in_stock") is not False]
    return filtered


async def search_products_tool(args: dict, api_key: str, session_id: str = "") -> dict:
    """دالة أداة متوافقة مع app.tool_loop.ToolFunc — تُربط بـ api_key/session_id
    الخاصين بطلب العميل الحالي عبر functools.partial وقت التسجيل بـ
    run_with_tools (انظر app/features/sales/router.py)، فما يمران بـ args
    التي يرسلها النموذج.

    args:
      - query: نص البحث — **يُقرأ ويُهمَل** (انظر تعليق الملف/_filter_catalog).
      - top_k: غير مستخدَم بعد الآن — الكتالوج الكامل هو الناتج، مقصوصاً
        فقط بسقف settings.max_injected_records (حماية ميزانية التوكِن، لا
        رغبة الموديل بعدد نتائج).
      - category: اسم فئة (catalog.categories) لتضييق رد هذا الدور — اختياري.
      - in_stock_only: true لعرض المتوفر بالمخزون فقط بهذا الدور — اختياري.
    """
    catalog = sessions.cached_catalog(session_id) if session_id else None
    if catalog is None:
        catalog = await product_repository.list_all(api_key)
        if session_id:
            sessions.cache_catalog(session_id, catalog)

    category = str(args.get("category") or "").strip()
    in_stock_only = bool(args.get("in_stock_only", False))
    result = _filter_catalog(catalog, category, in_stock_only)
    result = cap_for_model(result, settings.max_injected_records, label="search_products_tool")

    if not result:
        return {"results": [], "message": "ماكو منتج مطابق بالكتالوج."}
    return {"results": result}
