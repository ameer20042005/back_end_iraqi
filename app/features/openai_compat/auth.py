"""API-key authentication owned by the OpenAI-compatible feature."""

from fastapi import Header, HTTPException

from app.config import settings


async def require_openai_compat_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> str:
    if x_api_key != settings.openai_compat_api_key:
        raise HTTPException(
            status_code=401,
            detail="مفتاح API غير صحيح لواجهة OpenAI",
        )
    return x_api_key
