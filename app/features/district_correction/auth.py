"""API-key authentication owned by the district correction service."""

import hmac

from fastapi import Header, HTTPException

from app.config import settings


async def require_district_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> str:
    """Require this service's independent key on every correction request."""
    if not settings.district_api_key:
        raise HTTPException(
            status_code=500,
            detail="مفتاح خدمة تصحيح المناطق غير مضبوط بإعدادات الخادم",
        )
    if not hmac.compare_digest(x_api_key.encode("utf-8"), settings.district_api_key.encode("utf-8")):
        raise HTTPException(
            status_code=401,
            detail="مفتاح API غير صحيح لخدمة تصحيح المناطق",
        )
    return x_api_key
