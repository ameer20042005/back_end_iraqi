# -*- coding: utf-8 -*-
"""الطبقة المشتركة للاتصال بباك اند السستم — يستعملها app/products.py
وapp/order_gateway.py:

- `SystemBackendUnavailable`: استثناء موحّد لفشل الاتصال/الخادم/الرفض، حتى
  يتعامل المستدعي (الراوترات) مع "الخدمة غير متوفرة" بشكل واحد بدل ما
  ينكسر كل استدعاء بخطأ httpx خام يظهر للعميل كـ500 عارية.
- `get_client()`: عميل HTTP واحد مشترك (keep-alive) بدل عميل جديد لكل نداء.
- `request()`: يغلّف كل نداء بمعالجة موحّدة للأخطاء (اتصال، 5xx، **و4xx**).
- `auth_headers()`: ترويسات المصادقة (مفتاح الخدمة + توكن المستخدم الأصلي).

انظر docs/fix-plan.md § المرحلة 7 (الأعطال A5 · A6 · B6)."""

import logging
from contextvars import ContextVar
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# توكن المستخدم الأصلي (JWT) الذي وصل مع طلب /support/chat من jbot، محفوظ
# بسياق الطلب الحالي ليُمرَّر كما هو لباك اند السستم.
#
# ليش ContextVar مو معامل دالة؟ لأن api_key يمر عبر ~10 دوال بين الراوتر
# وorder_gateway (_deterministic_status_answer، _list_all_cached، بناء الاستعلام،
# ...). إضافة معامل ثانٍ لكل واحدة تعديل واسع بلا فائدة. ContextVar يُضبط مرة
# وحدة بأول الراوتر ويُقرأ بـ auth_headers() تحت — وكل طلب asyncio عنده نسخته
# المعزولة من السياق، فما يتسرّب توكن مستخدم لطلب مستخدم ثانٍ. نفس فكرة
# UserContext (ThreadLocal) بجهة jbot.
caller_auth_token: ContextVar[Optional[str]] = ContextVar("caller_auth_token", default=None)


class SystemBackendUnavailable(Exception):
    """باك اند السستم غير قابل للوصول (تعذّر الاتصال، انتهت المهلة، رجّع خطأ
    خادم، أو رفض الطلب بمفتاح/توكن غير صالح). الراوترات تلتقطها وترجع رداً
    مفهوماً للمستخدم بدل 500 عارية."""


# عميل واحد على مستوى الوحدة بدل AsyncClient جديد لكل استدعاء (العطل B6).
# السبب: كل `async with httpx.AsyncClient(...)` يفتح pool ويعمل TLS handshake
# من الصفر ثم يرميه — مع كل رسالة دعم/مبيعات. عميل مشترك يعيد استخدام
# الاتصالات (keep-alive) ويمنع استنفاد المنافذ تحت حمل. يُنشأ كسولاً عند أول
# نداء (لا وقت الاستيراد) حتى لا يلتقط event loop خاطئاً بالاختبارات، ويُغلق
# من lifespan بـ app/main.py عبر close_client().
_client: Optional[httpx.AsyncClient] = None


def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=settings.system_backend_base_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
        )
    return _client


async def close_client() -> None:
    """تُستدعى من lifespan بـ app/main.py عند الإغلاق."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def request(client: httpx.AsyncClient, method: str, path: str, **kwargs) -> httpx.Response:
    """يغلّف طلب httpx بمعالجة موحّدة لأخطاء الاتصال/الخادم/الرفض.

    يلتقط 4xx كمان، لا 5xx فقط (العطل A5): مفتاح API منتهي/مدوَّر أو توكن
    مستخدم غائب يرجّع 401 من jbot؛ سابقاً كان يمر من هنا بسلام ثم يرمي
    resp.raise_for_status() بالمستودع HTTPStatusError لا يلتقطه أحد → 500
    عارية للعميل بأسوأ لحظة. نستثني 404 لأن المزوّدين يعالجونه كـ"غير موجود"
    (نتيجة مشروعة) لا كعطل. المستدعون ما عادوا يحتاجون raise_for_status():
    أي رد يرجع من هنا هو 2xx/3xx أو 404."""
    try:
        resp = await client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise SystemBackendUnavailable(f"تعذّر الاتصال بباك اند السستم: {exc}") from exc
    if resp.status_code == 404:
        return resp
    if resp.status_code >= 500:
        raise SystemBackendUnavailable(
            f"باك اند السستم رجّع خطأ خادم ({resp.status_code})"
        )
    if resp.status_code >= 400:
        logger.error(
            "باك اند السستم رفض الطلب %s %s — %s: %s",
            method, path, resp.status_code, resp.text[:200],
        )
        raise SystemBackendUnavailable(
            f"باك اند السستم رفض الطلب ({resp.status_code}) — تحقق من مفتاح API"
        )
    return resp


def auth_headers(api_key: str) -> dict:
    """ترويسات كل نداء لباك اند السستم: مفتاح الخدمة دائماً، وتوكن المستخدم
    الأصلي إن وُجد بسياق الطلب (انظر caller_auth_token أعلاه).

    باك اند السستم الفعلي (jbot) يقرأ Authorization بفلتر JWT الخاص به
    ويستخرج منه الشركة والصلاحية — فيرجع بيانات شركة هذا المستخدم تحديداً،
    وهذا ما يخلي التوجيه متعدد الشركات يشتغل بلا أي منطق إضافي هنا."""
    headers = {"X-API-Key": api_key}
    token = caller_auth_token.get()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers
