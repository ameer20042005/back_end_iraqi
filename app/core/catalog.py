# -*- coding: utf-8 -*-
"""عميل باك اند البيانات "Catalog" (متاجر جيني) — منتجات، مخزون، وطلبات أونلاين.

نفس عقد `ai_agent` (Java: CatalogClient + CatalogAuthInterceptor):
  - كل طلب بهيدر `Authorization: Bearer <CATALOG_ACCESS_TOKEN>` و`X-Tenant-Id`.
  - القوائم ترجع PageResponse: {content, totalElements, totalPages, number, size}.
  - المسارات كلها إعدادات (`settings.catalog_path_*`)، ما ينكتب مسار بالكود.

الدوال هنا ترجع بيانات مقلَّصة (`trim_*`): بس الحقول اللي يحتاجها النموذج،
حتى يبقى السياق قصيراً وما يتسرّب شي ما له داعي. فحص ملكية الطلب (رقم الزبون)
مسؤولية المستدعي (أدوات المكالمة) — `same_phone()` هنا.

أي فشل يرفع `CatalogError` موحّد بسبب (`reason`) بدل أخطاء httpx خام.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_SIGNIFICANT_PHONE_DIGITS = 10
_MAX_PAGES = 5


class CatalogError(Exception):
    """reason: not_configured | bad_request | unauthorized | forbidden | not_found |
    conflict | unavailable | unexpected."""

    def __init__(self, reason: str, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.status = status


def configured() -> bool:
    return bool(settings.catalog_base_url)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    """عميل واحد مشترك (keep-alive) يُنشأ عند أول نداء."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.catalog_base_url,
            timeout=httpx.Timeout(settings.catalog_timeout_seconds, connect=min(5.0, settings.catalog_timeout_seconds)),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    if settings.catalog_access_token:
        headers["Authorization"] = f"Bearer {settings.catalog_access_token}"
    if settings.catalog_tenant_id:
        headers["X-Tenant-Id"] = settings.catalog_tenant_id
    return headers


def _reason(status: int) -> str:
    return {400: "bad_request", 422: "bad_request", 401: "unauthorized", 403: "forbidden",
            404: "not_found", 409: "conflict"}.get(status, "unavailable" if status >= 500 else "unexpected")


def _query(params: Dict[str, Any]) -> str:
    # ترميز صريح: "+" برقم الهاتف تصير %2B (مو مسافة)، والقيم الفارغة تنحذف.
    parts = [f"{quote(k, safe='')}={quote(str(v), safe='')}" for k, v in params.items() if v not in (None, "")]
    return ("?" + "&".join(parts)) if parts else ""


def _path(template: str, **ids: Any) -> str:
    for name, value in ids.items():
        template = template.replace("{" + name + "}", quote(str(_positive_id(value)), safe=""))
    return template


def _positive_id(value: Any) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise CatalogError("bad_request", f"معرّف غير صالح: {value!r}") from None
    if number <= 0:
        raise CatalogError("bad_request", f"معرّف غير صالح: {value!r}")
    return number


async def _request(method: str, path: str, params: Optional[Dict[str, Any]] = None,
                   json: Any = None) -> Any:
    if not configured():
        raise CatalogError("not_configured", "CATALOG_BASE_URL غير مضبوط")
    url = path + _query(params or {})
    try:
        resp = await get_client().request(method, url, headers=_headers(), json=json)
    except httpx.HTTPError as exc:
        raise CatalogError("unavailable", f"تعذّر الاتصال بباك اند البيانات: {exc}") from exc
    if resp.status_code >= 400:
        if resp.status_code != 404:
            logger.warning("Catalog رفض %s %s — %s: %s", method, path, resp.status_code, resp.text[:200])
        raise CatalogError(_reason(resp.status_code), f"Catalog رجّع {resp.status_code}", resp.status_code)
    if not resp.content:
        return None
    try:
        return resp.json()
    except ValueError as exc:
        raise CatalogError("unexpected", "رد Catalog مو JSON") from exc


def page_items(data: Any) -> List[dict]:
    """محتوى PageResponse (أو قائمة مباشرة)، بلا عناصر غير كائنات."""
    items = data.get("content") if isinstance(data, dict) else data
    return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []


def has_next_page(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    pages, number = data.get("totalPages"), data.get("number")
    return isinstance(pages, int) and isinstance(number, int) and number + 1 < pages


# ---------------------------------------------------------------------------
# الهواتف
# ---------------------------------------------------------------------------

def phone_digits(phone: Optional[str]) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-_SIGNIFICANT_PHONE_DIGITS:]


def same_phone(first: Optional[str], second: Optional[str]) -> bool:
    """نفس PhoneNumbers.sameNumber بـ ai_agent: آخر 10 أرقام (07xx = +9647xx)."""
    a, b = phone_digits(first), phone_digits(second)
    return bool(a) and a == b


# ---------------------------------------------------------------------------
# النداءات
# ---------------------------------------------------------------------------

async def search_products(text: str, size: Optional[int] = None) -> List[dict]:
    data = await _request("GET", settings.catalog_path_product_search,
                          {"text": text, "page": 0, "size": size or settings.catalog_search_page_size})
    return page_items(data)


async def get_product(product_id: Any) -> dict:
    data = await _request("GET", _path(settings.catalog_path_product, id=product_id))
    if not isinstance(data, dict):
        raise CatalogError("not_found", "المنتج غير موجود")
    return data


async def get_categories() -> List[dict]:
    return page_items(await _request("GET", settings.catalog_path_categories))


async def get_stock_infos(product_id: Any) -> List[dict]:
    """كل stock infos للمنتج (لحد 5 صفحات)، ومفلترة بمخزن المتجر لو مضبوط."""
    path = _path(settings.catalog_path_product_stock, productId=product_id)
    infos: List[dict] = []
    for page in range(_MAX_PAGES):
        data = await _request("GET", path, {"page": page, "size": settings.catalog_stock_page_size})
        infos += page_items(data)
        if not has_next_page(data):
            break
    storage = settings.catalog_storage_id
    return [i for i in infos if not storage or str(i.get("storageId")) == storage]


async def get_customer_orders(phone: str) -> List[dict]:
    """طلبات رقم واحد. فلتر Catalog جزئي (partial match)، فنعيد الفلترة بآخر 10 أرقام."""
    if not phone_digits(phone):
        return []
    data = await _request("GET", settings.catalog_path_customer_orders,
                          {"customerPhoneNumber": phone, "page": 0, "size": settings.catalog_orders_page_size})
    return [o for o in page_items(data) if same_phone(o.get("customerPhoneNumber"), phone)]


async def get_order(order_id: Any) -> dict:
    data = await _request("GET", _path(settings.catalog_path_order, id=order_id))
    if not isinstance(data, dict):
        raise CatalogError("not_found", "الطلب غير موجود")
    return data


# فلاتر قائمة /online/sells/all المدعومة فعلاً بـ Catalog (OnlineSellController):
# اسم الوسيط بالأداة ← اسم المعامل بالـ API.
ORDER_SEARCH_PARAMS = {
    "phone": "customerPhoneNumber",
    "receipt_number": "receiptNumber",
    "created_at": "createdAt",        # يوم واحد yyyy-MM-dd (UTC)، ماكو مدى تواريخ
    "flow_step_id": "flowStepId",     # الحالة بـ Catalog خطوة سير عمل (رقم)، ماكو فلتر حالة نصي
}


async def search_orders(**filters: Any) -> List[dict]:
    params = {api: filters.get(arg) for arg, api in ORDER_SEARCH_PARAMS.items()}
    params.update(page=0, size=settings.catalog_orders_page_size)
    return page_items(await _request("GET", settings.catalog_path_orders_search, params))


async def post_call_outcome(outcome: dict) -> bool:
    """إرسال نتيجة المكالمة لـ Catalog لو CATALOG_PATH_CALL_OUTCOME مضبوط."""
    if not configured() or not settings.catalog_path_call_outcome:
        return False
    await _request("POST", settings.catalog_path_call_outcome, json=outcome)
    return True


# ---------------------------------------------------------------------------
# تقليص الردود للنموذج
# ---------------------------------------------------------------------------

def _compact(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v not in (None, "", [], {})}


def _name(obj: Any) -> Optional[str]:
    if isinstance(obj, dict):
        return obj.get("nameAr") or obj.get("name") or obj.get("code")
    return obj if isinstance(obj, str) else None


def trim_order(order: dict, include_customer: bool = False) -> dict:
    items = [_compact({
        "product": i.get("productName"),
        "variant": i.get("variantTitle"),
        "quantity": i.get("quantity"),
        "unit_price": i.get("unitNetPrice") or i.get("price"),
    }) for i in order.get("items") or [] if isinstance(i, dict)]
    trimmed = {
        "order_id": order.get("id"),
        "receipt_number": order.get("receiptNumber"),
        "status": order.get("sellStatus") or order.get("status"),
        "step": _name(order.get("currentStep")),
        "stage": _name(order.get("currentStage")),
        "total": order.get("totalNetAmount") or order.get("totalPrice"),
        "payment_method": order.get("paymentMethod"),
        "created_at": order.get("createdAt"),
        "estimated_delivery": order.get("estimatedDeliveryDate"),
        "items": items,
    }
    if include_customer:
        trimmed["customer_name"] = order.get("customerName")
        trimmed["customer_phone"] = order.get("customerPhoneNumber")
    return _compact(trimmed)


def trim_stock(info: dict) -> dict:
    quantity = info.get("stockQuantity") or 0
    return _compact({
        "variant": info.get("variantTitle"),
        "price": info.get("regularActivePrice"),
        "in_stock": quantity > 0,
    })


def trim_product(product: dict, stock: Optional[List[dict]] = None) -> dict:
    trimmed = {
        "product_id": product.get("id"),
        "name": product.get("name"),
        "category": product.get("categoryName"),
    }
    if stock is not None:
        options = [trim_stock(s) for s in stock]
        trimmed["in_stock"] = any(o.get("in_stock") for o in options)
        prices = [o["price"] for o in options if isinstance(o.get("price"), (int, float))]
        if prices:
            trimmed["price"] = min(prices) if len(set(prices)) == 1 else {"from": min(prices), "to": max(prices)}
        if len(options) > 1:
            trimmed["variants"] = options[:6]
    return _compact(trimmed)


async def search_products_with_stock(text: str) -> List[dict]:
    """بحث + توفّر/سعر كل نتيجة (نداء stock لكل منتج، متوازية). فشل المخزون لمنتج
    واحد ما يفشّل البحث كله — يرجع المنتج بلا سعر."""
    products = await search_products(text)

    async def with_stock(product: dict) -> dict:
        try:
            return trim_product(product, await get_stock_infos(product.get("id")))
        except CatalogError:
            return trim_product(product)

    return list(await asyncio.gather(*(with_stock(p) for p in products)))
