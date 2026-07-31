# -*- coding: utf-8 -*-
"""استثناء مشترك لفشل الاتصال بباك اند السستم — يستعمله app/products.py
و app/order_gateway.py حتى يتعامل المستدعي (الراوترات) مع "الخدمة غير
متوفرة" بشكل موحّد، بدل ما ينكسر كل استدعاء بخطأ httpx خام يظهر للعميل
كـ500 عارية."""

import httpx


class SystemBackendUnavailable(Exception):
    """باك اند السستم غير قابل للوصول (تعذّر الاتصال، انتهت المهلة، أو رجّع
    خطأ خادم). الراوترات تلتقطها وترجع رداً مفهوماً للمستخدم بدل 500 عارية."""


async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
    """يغلّف طلب httpx بمعالجة موحّدة لأخطاء الاتصال/الخادم."""
    try:
        resp = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise SystemBackendUnavailable(f"تعذّر الاتصال بباك اند السستم: {exc}") from exc
    if resp.status_code >= 500:
        raise SystemBackendUnavailable(
            f"باك اند السستم رجّع خطأ خادم ({resp.status_code})"
        )
    return resp
