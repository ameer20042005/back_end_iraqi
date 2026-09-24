"""API-key authentication owned by the sales feature."""

from fastapi import Header, HTTPException

from app.config import settings


async def require_sales_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> str:
    if not settings.sales_api_key:
        raise HTTPException(
            status_code=500,
            detail="مفتاح خدمة المبيعات غير مضبوط بإعدادات الخادم",
        )
    if x_api_key != settings.sales_api_key:
        raise HTTPException(status_code=401, detail="مفتاح API غير صحيح لخدمة المبيعات")
    return x_api_key
