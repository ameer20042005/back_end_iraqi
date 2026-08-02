# -*- coding: utf-8 -*-
"""حماية كل خدمة بمفتاح API خاص بها من .env.

كل خدمة (مبيعات، دعم، إنشاء طلبات) عندها مفتاحها المستقل
(SALES_API_KEY / SUPPORT_API_KEY / ORDERS_API_KEY) — يُرسَل بهيدر X-API-Key
مع كل طلب لتلك الخدمة تحديداً. هذا يسمح بإلغاء صلاحية خدمة وحدها أو توزيع
مفتاح مختلف لكل عميل يستهلك خدمة معينة، بلا ما يأثر على البقية.

الاستخدام (انظر app/features/*/router.py):
    from app.auth import require_sales_api_key
    @router.post("/chat")
    async def sales_chat(req: ..., api_key: str = Depends(require_sales_api_key)):
        ...
"""

from fastapi import Header, HTTPException

from app.config import settings

_HEADER_NAME = "X-API-Key"


def _make_dependency(service_name: str, expected_key: str):
    """يبني دالة FastAPI dependency تتحقق من هيدر X-API-Key مقابل مفتاح خدمة
    محدد. كل خدمة تستدعي نسختها الخاصة (require_sales_api_key,
    require_support_api_key, require_orders_api_key) — لا دالة عامة واحدة،
    حتى يبقى مفتاح كل خدمة معزولاً تماماً عن البقية."""

    async def dependency(x_api_key: str = Header(..., alias=_HEADER_NAME)) -> str:
        if not expected_key:
            raise HTTPException(
                status_code=500,
                detail=f"مفتاح خدمة {service_name} غير مضبوط بإعدادات الخادم (.env)",
            )
        if x_api_key != expected_key:
            raise HTTPException(status_code=401, detail=f"مفتاح API غير صحيح لخدمة {service_name}")
        return x_api_key

    return dependency


require_sales_api_key = _make_dependency("المبيعات", settings.sales_api_key)
require_support_api_key = _make_dependency("الدعم", settings.support_api_key)
require_orders_api_key = _make_dependency("إنشاء الطلبات", settings.orders_api_key)
require_voice_followup_api_key = _make_dependency("المتابعة الصوتية", settings.voice_followup_api_key)
