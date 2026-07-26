# -*- coding: utf-8 -*-
"""ذاكرة محادثة بسيطة بالذاكرة (in-memory) — لكل جلسة تاريخ محدود من الأدوار.

ملاحظة: تُمسح عند إعادة تشغيل الخادم، وتصلح فقط مع --workers 1 (كما في
start.sh) لأنها غير مشتركة بين عدة عمليات.
"""

from collections import defaultdict
from typing import Dict, List

_MAX_TURNS = 12  # آخر N رسالة (مستخدم + مساعد) تُرسل كسياق
_MAX_PRODUCTS = 12  # آخر N منتج ظهر بالجلسة يبقى مرجعاً موثوقاً للدروع

_sessions: Dict[str, List[Dict[str, str]]] = defaultdict(list)
# منتجات ظهرت بأي دور سابق من نفس الجلسة — انظر remember_products().
_session_products: Dict[str, List[dict]] = defaultdict(list)
# آخر موقع ذكره العميل بهذه الجلسة — انظر remember_location().
_session_location: Dict[str, dict] = {}


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
