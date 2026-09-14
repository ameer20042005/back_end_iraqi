# -*- coding: utf-8 -*-
"""أداة بحث/تحميل كتالوج المنتجات — متاحة لوكيل المبيعات عبر app/tool_loop.py.

**تصميم "حمّل مرة وحدة"**: أول استدعاء بأي جلسة مبيعات يجيب الكتالوج
**كاملاً** من باك اند السستم (app/products.py::list_all) ويخزّنه بكاش الجلسة
(app/sessions.py::cache_catalog، بـTTL) — استدعاءات لاحقة بنفس الجلسة تلگى
الكاش مباشرة بلا أي طلب HTTP جديد. الكتالوج يُحقن بكل رسالة لاحقة (مقصوصاً
بسقف settings.max_injected_records، انظر app/context_blocks.py::
catalog_context_block).

**لكن `query` صارت تعمل فعلاً** (docs/fix-plan.md § المرحلة 8 — العطل B2):
سابقاً كانت الأداة تقرأ query وتتجاهلها عمداً وترجع نفس الكتالوج المقصوص —
فلما ما يلگى الموديل المنتج بالكتالوج المحقون (لأنه انقصّ) ويستدعي الأداة،
يحصل على نفس المقصوص وينفي مرتين. الآن الأداة تطابق نصياً على الكتالوج
**الكامل** من الكاش (لا المقصوص) — وهنا بالضبط قيمتها: توصل لما لم يُحقن.

ماكو مسار منتجات بباك اند السستم الفعلي بعد (docs/contract-matching.md)،
فالبحث محلي على الكاش لحين توفره؛ الانتقال للبحث بالخادم يصير هنا حصراً.

الفلاتر (category/in_stock_only) مبنية على سكيما جيني ستورز الحقيقية —
catalog.categories وcatalog.stock_info — وتُطبَّق محلياً على الكاش لتقصير
رد **هذا الدور تحديداً**؛ الكاش نفسه يبقى كاملاً بلا مساس."""

from typing import List

from app import sessions
from app.config import settings
from app.context_blocks import cap_for_model
from app.products import product_repository
from app.text_norm import normalize


def _match_query(catalog: List[dict], query: str) -> List[dict]:
    """مطابقة نصية على الكتالوج المخزَّن — إصلاح العطل B2 (query المتجاهَلة).

    نستخدم normalize (app/text_norm.py) لأن العربي يُكتب بصيغ مختلفة (همزات،
    تشكيل، أرقام هندية) — المطابقة الخام تفشل بلا تطبيع. نطابق على الاسم
    والفئة والوصف معاً: الزبون قد يسمي المنتج بكلمة من وصفه لا باسمه الرسمي.
    كلمات أقصر من 3 أحرف تُهمَل (حروف جر وأدوات: «من»، «لي») حتى لا تطابق
    كل شيء. استعلام فارغ = الكتالوج كله (سلوك "حمّل الكتالوج" الأصلي)."""
    q = normalize(query).strip()
    if not q:
        return catalog
    terms = [t for t in q.split() if len(t) > 2]
    if not terms:
        return catalog
    hits = []
    for p in catalog:
        haystack = normalize(
            f"{p.get('name', '')} {p.get('category', '')} {p.get('description', '')}"
        )
        if any(t in haystack for t in terms):
            hits.append(p)
    return hits


def _filter_catalog(catalog: List[dict], category: str, in_stock_only: bool) -> List[dict]:
    """تضييق محلي اختياري على الكتالوج المخزَّن — لا يمس الكاش نفسه، فقط
    رد هذا الدور."""
    filtered = catalog
    if category:
        filtered = [p for p in filtered if (p.get("category") or "") == category]
    if in_stock_only:
        filtered = [p for p in filtered if p.get("in_stock") is not False]
    return filtered


async def load_catalog(api_key: str, session_id: str = "") -> List[dict]:
    """الكتالوج الكامل لهذي الجلسة: من الكاش إن وُجد (ولم ينتهِ TTL)، وإلا
    من باك اند السستم مع تخزينه.

    عامة لأن app/features/sales/router.py يستدعيها **استباقياً** بأول رسالة
    منتج (العطل B5 — فجوة الدور الأول): بدل ما يتوقف الجواب على أن يقرر
    الموديل استدعاء الأداة (سلوك موثَّق أنه غير موثوق)، الكتالوج يكون محقوناً
    من الرسالة الأولى. ترمي SystemBackendUnavailable لو الخادم مطفأ —
    المستدعي يلتقطها ويرد برسالة مفهومة."""
    catalog = sessions.cached_catalog(session_id) if session_id else None
    if catalog is None:
        catalog = await product_repository.list_all(api_key)
        if session_id:
            sessions.cache_catalog(session_id, catalog)
    return catalog


async def search_products_tool(args: dict, api_key: str, session_id: str = "") -> dict:
    """دالة أداة متوافقة مع app.tool_loop.ToolFunc — تُربط بـ api_key/session_id
    الخاصين بطلب العميل الحالي عبر functools.partial وقت التسجيل بـ
    run_with_tools (انظر app/features/sales/router.py)، فما يمران بـ args
    التي يرسلها النموذج.

    args:
      - query: نص البحث — يُطابَق على اسم/فئة/وصف المنتج (انظر _match_query).
        فارغ = الكتالوج كله.
      - category: اسم فئة (catalog.categories) لتضييق رد هذا الدور — اختياري.
      - in_stock_only: true لعرض المتوفر بالمخزون فقط بهذا الدور — اختياري.
      - top_k: غير مستخدَم — الرد مقصوص بسقف settings.max_injected_records
        فقط (حماية ميزانية التوكِن، لا رغبة الموديل بعدد نتائج).

    شكل الرد (كل حقل مقصود):
      found   — هل اكو مطابقة أصلاً؛ False = الموديل يقول «ماكو» **بثقة مبنية
                على بحث فعلي** لا على قصّ عشوائي.
      count   — العدد الكلي للمطابقات قبل القصّ (إصلاح العطل B1).
      showing — كم وصل فعلاً بهذا الرد.
      results — العناصر نفسها (تبقى بالاسم نفسه: static/index.html وحرّاس
                app/guards.py عبر _tool_reference_text يقرؤونها).
      hint    — تعليمة عربية صريحة عند وجود بقية: أوامر الأداة تصل الموديل
                كنص، والموديل مضبوط على العراقي فيمتثل لها أفضل من علم منطقي."""
    catalog = await load_catalog(api_key, session_id)

    category = str(args.get("category") or "").strip()
    in_stock_only = bool(args.get("in_stock_only", False))
    matched = _match_query(catalog, str(args.get("query") or ""))
    filtered = _filter_catalog(matched, category, in_stock_only)
    if not filtered:
        return {"found": False, "count": 0, "results": [], "message": "ماكو منتج مطابق بالكتالوج"}

    results, total = cap_for_model(filtered, settings.max_injected_records, label="search_products_tool")
    reply = {"found": True, "count": total, "showing": len(results), "results": results}
    if total > len(results):
        reply["hint"] = (
            f"هذي {len(results)} من أصل {total} منتج مطابق. لا تدّعي إنها كل "
            "النتائج — ضيّق البحث بـ query أدق أو category للباقي."
        )
    return reply
