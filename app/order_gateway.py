# -*- coding: utf-8 -*-
"""بوابة نظام إدارة الطلبات — استعلام/إرسال حقيقي لحظي، بلا أي تخزين محلي.

واجهتان منفصلتان لكل اتجاه بيانات:
- **إخراج (Outbound)** — `OrderStatusProvider`: نستعلم منه (تتبع حالة طلب
  برقم الطلب أو الهاتف). يستخدمها `app/features/support/`.
- **إدخال (Inbound)** — `OrderSubmitter`: نرسل له طلباً جديداً بعد ما تحسبه
  `app/features/sales/service.py` (أو `order_intake`).

**لا تخزين محلي بأي شكل**: كل استدعاء يذهب فوراً لباك اند السستم عبر HTTP،
مصادقاً بمفتاح API (انظر app/auth.py)، والنتيجة تُعالَج وتُرجَع للمتصل بلا
أي احتفاظ بها هنا. البيانات تصل لحظياً وقت الاستدعاء فقط.

**التحقق من الشكل (إخراج فقط)**: كل استجابة تمر عبر `SystemOrder`
(`app/system_backend_schema.py`) — العقد الموثَّق رسمياً بـ
API.md § "عقد باك اند السستم". طلب لا يطابق العقد يُستبعَد بصمت من القوائم
(`search_by_phone`/`search_by_status`/`list_all`) أو يرجع None بـ
`get_by_order_id`، بدل ما يمرر شكلاً غير متوقَّع للموديل أو للرد الحتمي.

TODO: رابط ومسارات باك اند السستم الفعلية غير معروفة بعد — `SYSTEM_BACKEND_BASE_URL`
(app/config.py) ومسارات `search`/`_headers` أدناه أفضل تخمين موثَّق. عدّلها
فقط عند توفر التفاصيل الحقيقية؛ الواجهة (OrderStatusProvider/OrderSubmitter)
والمستدعين (support/sales) لا يتغيّرون.
"""

import logging
from abc import ABC, abstractmethod
from datetime import date, datetime
from typing import List, Optional

from pydantic import ValidationError

from app.config import settings
from app.order_query import OrderQuery, PagedOrders
from app.order_schema import OrderConfirmation
from app.system_backend import auth_headers, get_client, request as backend_request
from app.system_backend_schema import (
    SystemOrder,
    SystemOrderCountResponse,
    SystemOrderHistoryResponse,
)
from app.text_norm import normalize

logger = logging.getLogger(__name__)


def created_at_in_range(
    created_at: Optional[str], date_from: Optional[str], date_to: Optional[str]
) -> bool:
    """هل تاريخ إنشاء الطلب (ISO 8601) يقع بفترة [date_from, date_to]؟

    نتساهل عمداً مع أي خطأ تحليل (تنسيق غير متوقَّع من باك اند السستم
    الحقيقي، أو created_at مفقود): **نُبقي** الطلب بدل ما نخفيه — إخفاء طلب
    حقيقي عن الموظف بسبب فلتر تاريخ فشل تحليله أسوأ من عرضه بلا فلترة.

    عامة (بلا شرطة سفلية) لأنها تُستعمل أيضاً بـ
    app/features/support/router.py::_bulk_query_answer لفلترة دفتر الطلبات
    الكامل محلياً بفترة تاريخ — نفس منطق التحقق، مصدر وحيد.

    المقارنة على مستوى **اليوم** فقط (`.date()`) لا الوقت الكامل: date_from/
    date_to أصلاً "YYYY-MM-DD" بلا توقيت، فمقارنتهما بـdatetime كامل تفشل
    بـTypeError (offset-aware مقابل offset-naive) لو created_at جا بصيغة
    UTC صريحة ("...Z") من باك اند السستم — احتمال وارد جداً بـISO 8601
    الحقيقي، وليس مجرد حافة نادرة."""
    if not created_at:
        return True
    try:
        created_date = datetime.fromisoformat(created_at.replace("Z", "+00:00")).date()
        if date_from and created_date < date.fromisoformat(date_from):
            return False
        if date_to and created_date > date.fromisoformat(date_to):
            return False
        return True
    except (ValueError, TypeError):
        return True


def _parse_orders(raw_orders: list) -> List[dict]:
    """يتحقق من كل طلب مقابل SystemOrder ويرجعه كـ dict. طلب لا يطابق
    العقد يُستبعَد بصمت مع تحذير باللوق — انظر تعليق الملف أعلاه."""
    parsed: List[dict] = []
    for item in raw_orders:
        try:
            parsed.append(SystemOrder.model_validate(item).model_dump())
        except ValidationError as exc:
            logger.warning("عنصر طلب لا يطابق عقد SystemOrder، تم استبعاده: %s", exc)
    return parsed


def _digits(text) -> str:
    """رقم هاتف بصيغة قياسية للمقارنة: أرقام فقط، بلا مقدمة دولية (+964 /
    00964 / 964) وبصفر بادئ — نفس قاعدة extract_phone بـ support/router.py،
    حتى يطابق "+964 770 123 4567" الراجع من الخادم "07701234567" من الموظف."""
    digits = "".join(ch for ch in normalize(str(text or "")) if ch.isdigit())
    if not digits:
        return ""
    digits = digits.removeprefix("00").removeprefix("964")
    return digits if digits.startswith("0") else "0" + digits


def filter_orders_locally(orders: List[dict], query: OrderQuery) -> List[dict]:
    """يطبّق كل معايير OrderQuery (عدا limit/offset) على قائمة طلبات محلياً.

    الخطة البديلة (docs/fix-plan.md § المرحلة 2): باك اند السستم الفعلي
    (jbot، انظر docs/contract-matching.md) لا يدعم limit/offset ولا فلاتر
    المدينة/الاسم، فالفلترة المركّبة تُنفَّذ **هنا داخل طبقة المستودع حصراً**
    على ما رجّعه الخادم. هذا لا يحل مشكلة الشبكة، لكنه يحمي الموديل بالكامل
    (العطل B1): الأداة والراوتر يتعاملان مع OrderQuery/PagedOrders فقط.

    المطابقة بالاحتواء بعد normalize (لا مساواة خام): الحالة تجي من
    extract_status كنص كامل («قيد التوصيل») لكن الموديل قد يرسل جزءاً
    («توصيل»)، والمدينة تُكتب بهمزات مختلفة. رقم الهاتف يُقارن بالأرقام فقط
    (الخادم قد يرجّعه بفواصل أو بمقدمة دولية). فلتر التاريخ يعيد استعمال
    created_at_in_range — نفس منطق التحقق، مصدر وحيد.

    عامة (بلا شرطة سفلية) عمداً: مزوّد الاختبار الوهمي بـ
    tests/test_support_queries.py يستعملها حتى يطابق سلوك المزوّد الحقيقي."""
    matched: List[dict] = []
    for order in orders:
        if query.order_id and normalize(str(order.get("order_id") or "")) != normalize(query.order_id):
            continue
        if query.phone and _digits(order.get("phone")) != _digits(query.phone):
            continue
        if query.status and normalize(query.status) not in normalize(str(order.get("status") or "")):
            continue
        if query.city and normalize(query.city) not in normalize(str(order.get("customer_city") or "")):
            continue
        if query.customer_name and normalize(query.customer_name) not in normalize(
            str(order.get("customer_name") or "")
        ):
            continue
        if (query.date_from or query.date_to) and not created_at_in_range(
            order.get("created_at"), query.date_from, query.date_to
        ):
            continue
        matched.append(order)
    return matched


# ---------------------------------------------------------------------------
# إخراج (Outbound) — استعلام حالة طلب
# ---------------------------------------------------------------------------


class OrderStatusProvider(ABC):
    @abstractmethod
    async def get_by_order_id(self, order_id: str, api_key: str) -> Optional[dict]:
        """يرجع حالة طلب واحد بمعرّفه، أو None إذا غير موجود."""

    @abstractmethod
    async def search_by_phone(
        self,
        phone: str,
        api_key: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[dict]:
        """يرجع كل الطلبات المرتبطة برقم هاتف، بفلترة اختيارية بفترة تاريخ
        (ISO 8601، مثل "2026-07-01") — البحث الأساسي بميزة الدعم صار برقم
        الهاتف (الزبون يتذكره، لا رقم الطلب)، مع مرونة تحديد فترة زمنية
        («طلبات هذا الرقم من الشهر الماضي»)."""

    @abstractmethod
    async def search_by_status(self, status: str, api_key: str) -> List[dict]:
        """يرجع كل الطلبات بحالة معينة («قيد التوصيل»، «تم التسليم»...).

        هذي عملية داخلية للموظفين (تتبع تشغيلي)، مو استعلام زبون — البوت
        داخلي بحت."""

    @abstractmethod
    async def list_all(self, api_key: str) -> List[dict]:
        """يرجع كل الطلبات — الآن فقط لاشتقاق الحالات/المندوبين الموجودين
        فعلاً بالبيانات (app/features/support/router.py::_known_statuses)؛
        لم يعد يُحقن بالبرومبت (docs/fix-plan.md § المرحلة 4)."""

    @abstractmethod
    async def search(self, query: OrderQuery, api_key: str) -> PagedOrders:
        """يرجع صفحة نتائج + العدد الكلي للمطابقات (لا عدد الصفحة).

        ميثود بحث واحدة بمعيار مركّب (app/order_query.py) بدل search_by_phone/
        search_by_status اللتين تدعم كل وحدة بُعداً واحداً — فسؤال مركّب
        («طلبات بغداد قيد التوصيل الشهر الماضي») ما كان يقدر يستعملهما، وهذا
        ما دفع التصميم القديم لحقن الدفتر كاملاً وترك البحث للموديل. limit
        مسقَّف بـ50 بالنموذج نفسه، فلا يمكن أن يرجع من هنا حقن ضخم (B1)."""

    @abstractmethod
    async def count(
        self,
        api_key: str,
        phone: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Optional[int]:
        ...

    @abstractmethod
    async def get_history(self, order_id: str, api_key: str) -> Optional[dict]:
        ...

    async def count_for_query(self, query: OrderQuery, api_key: str) -> int:
        """عدد المطابقات لمعايير OrderQuery — بلا جلب أي بيانات للمستدعي.

        يفضّل العدّ الحقيقي بالخادم (`count`، مسار /orders/count بـ jbot —
        COUNT بفهرس، ميلي ثانية مهما كبر الجدول) لما تكون كل الفلاتر مفهومة
        بجهته (phone/status/date). فلتر لا يفهمه الخادم (city/customer_name/
        order_id) أو رد عدّ غير صالح → نعدّ من `search().total` (الخطة
        البديلة). ميثود ملموسة على الواجهة عمداً: القرار "خادم أم محلي"
        واحد لكل المزوّدين، والمستدعون (الأداة والمسار الحتمي) يرون رقماً فقط."""
        server_side = not (query.city or query.customer_name or query.order_id)
        if server_side:
            total = await self.count(
                api_key,
                phone=query.phone, status=query.status,
                date_from=query.date_from, date_to=query.date_to,
            )
            if total is not None:
                return total
        page = await self.search(query.model_copy(update={"limit": 1, "offset": 0}), api_key)
        return page.total


class HttpOrderStatusProvider(OrderStatusProvider):
    """عميل HTTP رفيع لباك اند السستم — بلا أي تخزين أو فهرسة محلية."""

    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url or settings.system_backend_base_url
        self._timeout = timeout

    def _headers(self, api_key: str) -> dict:
        return auth_headers(api_key)

    async def _get(self, path: str, api_key: str, params: Optional[dict] = None) -> Optional[dict]:
        # العميل المشترك (system_backend.get_client) بدل AsyncClient جديد لكل
        # استدعاء — يعيد استخدام الاتصالات (keep-alive) ويمنع استنفاد المنافذ
        # تحت حمل (العطل B6). الترويسات تُمرَّر **لكل طلب** لا على العميل:
        # توكن المستخدم (Authorization) يختلف بين طلب وآخر، ووضعه على العميل
        # المشترك يعني كل المستخدمين على شركة واحدة — تسريب بيانات صامت.
        # نبني الرابط كاملاً حتى يبقى base_url المُمرَّر للمُنشئ شغّالاً رغم
        # أن العميل مشترك. ماكو raise_for_status: backend_request يلتقط كل
        # 4xx/5xx ويرمي SystemBackendUnavailable (العطل A5) — ما يرجع من هنا
        # إلا نجاح أو 404.
        resp = await backend_request(
            get_client(), "GET", self._base_url.rstrip("/") + path,
            params=params, headers=self._headers(api_key), timeout=self._timeout,
        )
        if resp.status_code == 404:
            return None
        return resp.json()

    async def get_by_order_id(self, order_id: str, api_key: str) -> Optional[dict]:
        data = await self._get(f"/orders/{order_id}", api_key)
        if data is None:
            return None
        try:
            return SystemOrder.model_validate(data).model_dump()
        except ValidationError as exc:
            logger.warning("طلب %s لا يطابق عقد SystemOrder: %s", order_id, exc)
            return None

    async def search_by_phone(
        self,
        phone: str,
        api_key: str,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> List[dict]:
        # نمرر فترة التاريخ لباك اند السستم (تخمين موثَّق — TODO أعلى الملف)
        # **و**نفلتر محلياً كمان على created_at: باك اند السستم قد يتجاهل
        # فلاتر غير معروفة له، فالفلترة المحلية شبكة أمان تضمن النتيجة صحيحة
        # بغض النظر هل الباك اند الحقيقي يدعم هذا الفلتر أصلاً أم لا.
        params = {"phone": phone}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        data = await self._get("/orders/search", api_key, params=params)
        orders = _parse_orders((data or {}).get("orders", []))
        if date_from or date_to:
            orders = [
                o for o in orders
                if created_at_in_range(o.get("created_at"), date_from, date_to)
            ]
        return orders

    async def search_by_status(self, status: str, api_key: str) -> List[dict]:
        data = await self._get("/orders/search", api_key, params={"status": status})
        return _parse_orders((data or {}).get("orders", []))

    async def list_all(self, api_key: str) -> List[dict]:
        data = await self._get("/orders", api_key)
        return _parse_orders((data or {}).get("orders", []))

    async def _fetch_candidates(self, query: OrderQuery, api_key: str) -> List[dict]:
        """أضيق مجموعة يقدر الخادم يرجّعها بفلاتره الموجودة فعلاً (رقم طلب →
        طلب واحد، هاتف/حالة → /orders/search بذاك الفلتر، وإلا الدفتر)؛
        الباقي يُفلتر محلياً بـ filter_orders_locally. هكذا ما ننقل الدفتر
        كله لسؤال فيه هاتف أو حالة. أسماء المعاملات التي يفهمها الخادم تبقى
        داخل search_by_* حصراً — نقطة التماس الوحيدة معه."""
        if query.order_id:
            order = await self.get_by_order_id(query.order_id, api_key)
            return [order] if order else []
        if query.phone:
            return await self.search_by_phone(
                query.phone, api_key, date_from=query.date_from, date_to=query.date_to
            )
        if query.status:
            return await self.search_by_status(query.status, api_key)
        return await self.list_all(api_key)

    async def search(self, query: OrderQuery, api_key: str) -> PagedOrders:
        # الخطة البديلة (docs/contract-matching.md § 2): القصّ لصفحة واحدة
        # يصير هنا — لا بالأداة ولا بالبرومبت — وtotal يُحسب من القائمة
        # المفلترة **قبل** القصّ، فيرجع للموديل العدد الكلي الحقيقي (بحدود ما
        # رجّعه الخادم). لما يدعم jbot limit/offset/total: استبدل جسم هذه
        # الدالة بنسخة الترقيم الحقيقي بـ docs/fix-plan.md § المرحلة 2 — ولا
        # شيء آخر بالمشروع يتغيّر.
        matched = filter_orders_locally(await self._fetch_candidates(query, api_key), query)
        return PagedOrders(
            orders=matched[query.offset: query.offset + query.limit],
            total=len(matched),
            offset=query.offset,
        )

    async def count(
        self,
        api_key: str,
        phone: Optional[str] = None,
        status: Optional[str] = None,
        date_from: Optional[str] = None,
        date_to: Optional[str] = None,
    ) -> Optional[int]:
        params = {}
        if phone:
            params["phone"] = phone
        if status:
            params["status"] = status
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        data = await self._get("/orders/count", api_key, params=params or None)
        if data is None:
            return None
        try:
            return SystemOrderCountResponse.model_validate(data).count
        except ValidationError as exc:
            logger.warning("رد العدّ لا يطابق عقد SystemOrderCountResponse: %s", exc)
            return None

    async def get_history(self, order_id: str, api_key: str) -> Optional[dict]:
        data = await self._get(f"/orders/{order_id}/history", api_key)
        if data is None:
            return None
        try:
            return SystemOrderHistoryResponse.model_validate(data).model_dump()
        except ValidationError as exc:
            logger.warning("سجل مراحل الطلب %s لا يطابق العقد: %s", order_id, exc)
            return None


order_status_provider: OrderStatusProvider = HttpOrderStatusProvider()


# ---------------------------------------------------------------------------
# إدخال (Inbound) — تثبيت طلب جديد بالنظام الخارجي
# ---------------------------------------------------------------------------


class OrderSubmitter(ABC):
    @abstractmethod
    async def submit(self, order: OrderConfirmation, api_key: str) -> bool:
        """يرسل طلباً مؤكَّداً للنظام الخارجي. يرجع True لو نجح الإرسال."""


class HttpOrderSubmitter(OrderSubmitter):
    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url or settings.system_backend_base_url
        self._timeout = timeout

    async def submit(self, order: OrderConfirmation, api_key: str) -> bool:
        # نفس تحويل HttpOrderStatusProvider._get: عميل مشترك، وبلا
        # raise_for_status لأن backend_request يرمي SystemBackendUnavailable لأي
        # 4xx/5xx (يلتقطه resolve_order بـ app/features/sales/service.py).
        await backend_request(
            get_client(), "POST", self._base_url.rstrip("/") + "/orders",
            json=order.model_dump(), headers={"X-API-Key": api_key}, timeout=self._timeout,
        )
        return True


order_submitter: OrderSubmitter = HttpOrderSubmitter()
