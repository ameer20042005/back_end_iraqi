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
