# -*- coding: utf-8 -*-
"""ذاكرة محادثة بسيطة بالذاكرة (in-memory) — لكل جلسة تاريخ محدود من الأدوار.

ملاحظة: تُمسح عند إعادة تشغيل الخادم، وتصلح فقط مع --workers 1 (كما في
start.sh) لأنها غير مشتركة بين عدة عمليات.
"""

import time
from collections import OrderedDict, defaultdict
from typing import Dict, List, Optional, Tuple

_MAX_TURNS = 12  # آخر N رسالة (مستخدم + مساعد) تُرسل كسياق
_MAX_PRODUCTS = 12  # آخر N منتج ظهر بالجلسة يبقى مرجعاً موثوقاً للدروع

_sessions: Dict[str, List[Dict[str, str]]] = defaultdict(list)
# منتجات ظهرت بأي دور سابق من نفس الجلسة — انظر remember_products().
_session_products: Dict[str, List[dict]] = defaultdict(list)
# آخر موقع ذكره العميل بهذه الجلسة — انظر remember_location().
_session_location: Dict[str, dict] = {}

# كاش نتائج بحث المنتجات (search_products_tool) بحدود الجلسة — مفتاح مركّب
# (session_id + نص الاستعلام والفلاتر) لأن كل استعلام مختلف عن الآخر، خلافاً
# لدفتر الطلبات اللي هو قائمة واحدة كاملة. انظر cached_product_search()/
# cache_product_search() بـ app/tools/products.py.
#
# ⚠️ لم تعد مستخدَمة من مسار المبيعات بعد الانتقال لتحميل الكتالوج كاملاً
# مرة وحدة بالجلسة (انظر cached_catalog()/cache_catalog() أدناه).
_MAX_PRODUCT_SEARCH_CACHE = 24  # أقصى عدد استعلامات مختلفة تبقى بذاكرة الجلسة
_session_product_search_cache: Dict[str, "OrderedDict[str, List[dict]]"] = defaultdict(OrderedDict)

# كاش الكتالوج الكامل (list_all) بحدود الجلسة — قائمة واحدة كاملة، لا مفتاح
# استعلام. يُجلب مرة وحدة أول ما تحتاجه جلسة مبيعات (app/tools/products.py::
# load_catalog) ثم يُحقن بكل رسالة لاحقة بنفس الجلسة (app/context_blocks.py::
# catalog_context_block عبر build_sales_prompt).
# TTL خمس دقائق (العطل B3): التبرير القديم لغياب الإبطال — «الكتالوج ما يتغيّر
# بفعل العميل» — صحيح، لكنه يتغيّر بفعل **زبائن آخرين**: آخر قطعة تنباع بجلسة
# ثانية، وبوتنا يظل يبيعها. 300 ثانية توازن معقول بين طزاجة المخزون وعدم
# إعادة جلب الكتالوج بكل رسالة.
_CATALOG_TTL_SECONDS = 300
_session_catalog_cache: Dict[str, Tuple[List[dict], float]] = {}


def get(session_id: str) -> List[Dict[str, str]]:
    return _sessions.get(session_id, [])


def append(session_id: str, role: str, content: str) -> None:
    history = _sessions[session_id]
    history.append({"role": role, "content": content})
    if len(history) > _MAX_TURNS:
        del history[: len(history) - _MAX_TURNS]


def remember_products(session_id: str, products: List[dict]) -> None:
    """يخزّن المنتجات اللي استرجعها RAG بهذا الدور.

    السبب: الدروع تقيس أرقام رد الوكيل على «المنتجات المسترجَعة»، وRAG يبني
    الاسترجاع من رسالة العميل الأخيرة وحدها. بآخر خطوة من أي طلب («ثبت
    الحجز، السماوة، 0781...») ما بالرسالة أي اسم منتج، فيرجع الاسترجاع فارغاً
    ويصير سعر اللابتوب المذكور قبل رسالتين «رقماً مختلَقاً» ويُحجب الرد.
    الاحتفاظ بمنتجات الجلسة يخلّي المرجع يمتد على المحادثة كلها بدل الدور
    الواحد — بدون توسيع ما يُسمح به (نفس منتجات الكتالوج، لا غير)."""
    known = _session_products[session_id]
    seen = {str(p.get("id")) for p in known}
    for product in products:
        pid = str(product.get("id"))
        if pid in seen:
            continue
        seen.add(pid)
        known.append(product)
    if len(known) > _MAX_PRODUCTS:
        del known[: len(known) - _MAX_PRODUCTS]


def known_products(session_id: str) -> List[dict]:
    """كل المنتجات اللي مرّت بهذه الجلسة — مرجع الدروع التراكمي."""
    return _session_products.get(session_id, [])


def remember_location(session_id: str, city: str = "", district: str = "") -> None:
    """يخزّن آخر محافظة/منطقة ذكرها العميل صراحةً بهذه الجلسة.

    السبب: العميل يذكر حيّه بأول المحادثة («اني من الحارثية») ثم تكمل
    المحادثة عشر رسائل بالمنتج والسعر. عند تثبيت الطلب يخرج الحي فارغاً
    لأنه طلع من نافذة _MAX_TURNS، فينحفظ الطلب بلا منطقة أو — أسوأ — يملأ
    الوكيل الفراغ بتخمين. الحقلان يُحدَّثان كلٌّ على حدة: ذكر منطقة جديدة
    ما لازم يمسح المحافظة المعروفة والعكس."""
    if not (city or district):
        return
    known = _session_location.setdefault(session_id, {"city": "", "district": ""})
    if city:
        known["city"] = city
    if district:
        known["district"] = district


def known_location(session_id: str) -> dict:
    """آخر موقع معروف بهذه الجلسة: {"city", "district"} (قيم فارغة إن ماكو)."""
    return _session_location.get(session_id, {"city": "", "district": ""})


def cached_product_search(session_id: str, cache_key: str) -> Optional[List[dict]]:
    """نتيجة بحث منتجات سابقة بنفس `cache_key` (استعلام+فلاتر) بهذه الجلسة،
    أو None إذا ما استُعلم عنها بعد بهذه الجلسة.

    السبب: النموذج قد يستدعي search_products بنفس الاستعلام أكثر من مرة
    بنفس المحادثة (يتأكد من السعر قبل الملخّص، ثم عند التثبيت) — بلا هذا
    الكاش كل استدعاء يجيب من باك اند السستم من الصفر رغم أن كتالوج المنتجات
    ما يتغيّر بين رسالتين متتاليتين من نفس الزبون."""
    return _session_product_search_cache[session_id].get(cache_key)


def cache_product_search(session_id: str, cache_key: str, results: List[dict]) -> None:
    """يخزّن نتيجة بحث منتجات بهذه الجلسة (انظر cached_product_search).

    بحد أقصى `_MAX_PRODUCT_SEARCH_CACHE` استعلاماً مختلفاً لكل جلسة —
    الأقدم يُطرح أولاً (LRU) حتى لا تكبر الذاكرة بلا حدود مع جلسة طويلة
    كثيرة الاستعلامات."""
    cache = _session_product_search_cache[session_id]
    cache[cache_key] = results
    cache.move_to_end(cache_key)
    if len(cache) > _MAX_PRODUCT_SEARCH_CACHE:
        cache.popitem(last=False)


def cached_catalog(session_id: str) -> Optional[List[dict]]:
    """الكتالوج الكامل المخزَّن بهذه الجلسة، أو None إذا ما انجاب بعد أو
    **انتهت صلاحيته** (_CATALOG_TTL_SECONDS) — يميّز "محفوظ وفاضي" (كتالوج
    حقيقي فارغ) عن "غير محفوظ أصلاً"."""
    entry = _session_catalog_cache.get(session_id)
    if entry is None:
        return None
    products, loaded_at = entry
    if time.monotonic() - loaded_at > _CATALOG_TTL_SECONDS:
        del _session_catalog_cache[session_id]
        return None
    return products


def cache_catalog(session_id: str, products: List[dict]) -> None:
    """يخزّن الكتالوج الكامل بهذه الجلسة مع وقت التحميل (انظر cached_catalog)
    — استدعاء وحد لكل جلسة عملياً ضمن الـTTL؛ الاستدعاءات اللاحقة
    (load_catalog بـ app/tools/products.py) تلگى الكاش بدل إعادة الجلب."""
    _session_catalog_cache[session_id] = (products, time.monotonic())
