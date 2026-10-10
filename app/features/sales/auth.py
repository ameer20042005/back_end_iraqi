"""API-key authentication owned by the sales feature."""

import hmac
from typing import Optional

from fastapi import Header, HTTPException

from app.config import settings


async def require_sales_api_key(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None),
) -> str:
    """يقبل `X-API-Key` أو `Authorization: Bearer <key>` (الصيغة التي يرسلها عملاء OpenAI/Spring AI)."""
    if not settings.sales_api_key:
        raise HTTPException(
            status_code=500,
            detail="مفتاح خدمة المبيعات غير مضبوط بإعدادات الخادم",
        )
    supplied = x_api_key
    if not supplied and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[7:].strip()
    if not supplied or not hmac.compare_digest(supplied.encode(), settings.sales_api_key.encode()):
        raise HTTPException(status_code=401, detail="مفتاح API غير صحيح لخدمة المبيعات")
    return supplied
