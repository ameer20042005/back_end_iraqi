# -*- coding: utf-8 -*-
"""أدوات بيانات ينفّذها الخادم بمساعد الموظف (operator.py) — من باك اند Catalog.

بخلاف أدوات التطبيق (اللي ترجع للتطبيق كـ tool_calls) هذه تتنفّذ هنا بحلقة
داخل نفس الطلب، والتطبيق ما يشوف استدعاءاتها أبداً — يشوف بس النتيجة النهائية
(نص، أو start_call/أدوات التطبيق). تنعرض للنموذج بس لما CATALOG_BASE_URL مضبوط.

الموظف من فريق الشركة، فنتائج الطلبات هنا تشمل اسم الزبون ورقمه (يحتاجهم
لـ start_call) — بخلاف أدوات المكالمة الصوتية.
"""

import json
import logging
import re
from typing import Any, Awaitable, Callable, Dict, List

from app.core import catalog

logger = logging.getLogger(__name__)

SEARCH_ORDERS = "search_orders"
SEARCH_PRODUCTS = "search_products"
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_UNAVAILABLE = "تعذّر الوصول لبيانات المتجر حالياً. بلّغ الموظف."


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


async def _search_orders(args: Dict[str, Any]) -> str:
    order_id = str(args.get("order_id") or "").strip()
    if order_id:
        try:
            order = await catalog.get_order(order_id)
        except catalog.CatalogError as exc:
            if exc.reason in ("not_found", "bad_request"):
                return "ماكو طلب بهذا الرقم."
            raise
        return _json({"count": 1, "orders": [catalog.trim_order(order, include_customer=True)]})

    filters = {
        "phone": str(args.get("phone") or "").strip() or None,
        "receipt_number": str(args.get("receipt_number") or "").strip() or None,
        "created_at": str(args.get("created_at") or "").strip() or None,
        "flow_step_id": args.get("flow_step_id"),
    }
    if filters["created_at"] and not _DATE_RE.fullmatch(filters["created_at"]):
        return "created_at لازم بصيغة YYYY-MM-DD."
    if filters["flow_step_id"] is not None:
        try:
            filters["flow_step_id"] = int(filters["flow_step_id"])
        except (TypeError, ValueError):
            return "flow_step_id لازم رقم."
    if not any(v is not None for v in filters.values()):
        return "حدّد فلتر واحد على الأقل: order_id أو phone أو receipt_number أو created_at أو flow_step_id."
    orders = await catalog.search_orders(**filters)
    if filters["phone"]:
        orders = [o for o in orders if catalog.same_phone(o.get("customerPhoneNumber"), filters["phone"])]
    if not orders:
        return "ماكو طلبات مطابقة."
    return _json({"count": len(orders), "orders": [catalog.trim_order(o, include_customer=True) for o in orders]})


async def _search_products(args: Dict[str, Any]) -> str:
    text = str(args.get("text") or "").strip()[:200]
    if not text:
        return "اكتب اسم المنتج."
    products = await catalog.search_products_with_stock(text)
    if not products:
        return "ماكو منتج مطابق."
    return _json({"count": len(products), "products": products})


TOOLS: List[dict] = [
    {"type": "function", "function": {
        "name": SEARCH_ORDERS,
        "description": (
            "Search the store's online orders (server-side data). Use order_id for one order, or filter by "
            "customer phone, receipt number, a single creation day (created_at) or workflow step id. "
            "Returns order id, status, step, total, items, customer name and phone."
        ),
        "parameters": {"type": "object", "properties": {
            "order_id": {"type": "string", "description": "Exact order id."},
            "phone": {"type": "string", "description": "Customer phone number."},
            "receipt_number": {"type": "string", "description": "Receipt number (partial match)."},
            "created_at": {"type": "string", "description": "One day, YYYY-MM-DD (UTC). Date ranges are not supported."},
            "flow_step_id": {"type": "integer", "description": "Workflow step id (the store's order status step)."},
        }},
    }},
    {"type": "function", "function": {
        "name": SEARCH_PRODUCTS,
        "description": "Search the store catalog (server-side data). Returns products with price and availability.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "Product name or words to search."},
        }, "required": ["text"]},
    }},
]

_HANDLERS: Dict[str, Callable[[Dict[str, Any]], Awaitable[str]]] = {
    SEARCH_ORDERS: _search_orders,
    SEARCH_PRODUCTS: _search_products,
}


def active_tools() -> List[dict]:
    return list(TOOLS) if catalog.configured() else []


def names() -> set:
    return {t["function"]["name"] for t in active_tools()}


async def run(name: str, arguments: Any) -> str:
    handler = _HANDLERS.get(name)
    if handler is None or not catalog.configured():
        return f"الأداة {name} غير موجودة."
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "وسائط الأداة مو JSON صالح."
    if not isinstance(arguments, dict):
        return "وسائط الأداة لازم تكون كائن JSON."
    try:
        return await handler(arguments)
    except catalog.CatalogError as exc:
        logger.warning("أداة الموظف %s فشلت: %s", name, exc)
        return _UNAVAILABLE
