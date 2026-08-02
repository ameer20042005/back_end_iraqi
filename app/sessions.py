# -*- coding: utf-8 -*-
"""ذاكرة محادثة بسيطة بالذاكرة (in-memory) — لكل جلسة تاريخ محدود من الأدوار.

ملاحظة: تُمسح عند إعادة تشغيل الخادم، وتصلح فقط مع --workers 1 (كما في
start.sh) لأنها غير مشتركة بين عدة عمليات.
"""

from collections import OrderedDict, defaultdict
from typing import Dict, List, Optional

_MAX_TURNS = 12  # آخر N رسالة (مستخدم + مساعد) تُرسل كسياق
_MAX_PRODUCTS = 12  # آخر N منتج ظهر بالجلسة يبقى مرجعاً موثوقاً للدروع

_sessions: Dict[str, List[Dict[str, str]]] = defaultdict(list)
# منتجات ظهرت بأي دور سابق من نفس الجلسة — انظر remember_products().
_session_products: Dict[str, List[dict]] = defaultdict(list)
# آخر موقع ذكره العميل بهذه الجلسة — انظر remember_location().
_session_location: Dict[str, dict] = {}
# كاش قائمة الطلبات الكاملة (list_all) بحدود الجلسة — انظر
# cached_orders()/cache_orders(). يُمسح فقط عند invalidate_orders() (بعد
# تثبيت طلب جديد) أو نهاية الجلسة نفسها؛ لا TTL زمني.
_session_orders_cache: Dict[str, List[dict]] = {}

# كاش نتائج بحث المنتجات (search_products_tool) بحدود الجلسة — مفتاح مركّب
# (session_id + نص الاستعلام والفلاتر) لأن كل استعلام مختلف عن الآخر، خلافاً
# لدفتر الطلبات اللي هو قائمة واحدة كاملة. انظر cached_product_search()/
# cache_product_search() بـ app/tools/products.py.
_MAX_PRODUCT_SEARCH_CACHE = 24  # أقصى عدد استعلامات مختلفة تبقى بذاكرة الجلسة
_session_product_search_cache: Dict[str, "OrderedDict[str, List[dict]]"] = defaultdict(OrderedDict)


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


def cached_orders(session_id: str) -> Optional[List[dict]]:
    """قائمة الطلبات الكاملة (list_all) المخزَّنة بهذه الجلسة، أو None إذا
    ما محفوظة بعد — يميّز "محفوظة وفاضية" عن "غير محفوظة أصلاً".

    السبب: _known_statuses تُستدعى من extract_status، والذي يُستدعى مرتين+
    بكل رسالة دعم واحدة (مرة للرسالة الحالية، ومرات لرسائل history عند
    المتابعة) — كل استدعاء كان يجيب *كل* الطلبات من باك اند السستم من
    الصفر. بحدود نفس الجلسة، دفتر الطلبات ما يتغيّر بين رسالتين متتاليتين
    من نفس الموظف، فلا داعي لإعادة الجلب إلا بعد تثبيت طلب جديد."""
    return _session_orders_cache.get(session_id)


def cache_orders(session_id: str, orders: List[dict]) -> None:
    """يخزّن قائمة الطلبات الكاملة بهذه الجلسة (انظر cached_orders)."""
    _session_orders_cache[session_id] = orders


def invalidate_orders(session_id: str) -> None:
    """يمسح كاش الطلبات بهذه الجلسة — يُستدعى بعد تثبيت طلب جديد حتى لا
    يبقى الجرد المخزَّن ناقصاً عن الواقع."""
    _session_orders_cache.pop(session_id, None)


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
