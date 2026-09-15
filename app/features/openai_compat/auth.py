"""API-key authentication owned by the OpenAI-compatible feature."""

from fastapi import Header, HTTPException


OPENAI_COMPAT_API_KEY = "sk-openai-7a9c2e4f6b1d8a0c3e5f7b9d1a3c5e7f"


async def require_openai_compat_api_key(
    x_api_key: str = Header(..., alias="X-API-Key"),
) -> str:
    if x_api_key != OPENAI_COMPAT_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="مفتاح API غير صحيح لواجهة OpenAI",
        )
    return x_api_key
