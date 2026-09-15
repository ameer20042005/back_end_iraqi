import asyncio

import pytest
from fastapi import HTTPException

from app.features.openai_compat.auth import (
    OPENAI_COMPAT_API_KEY,
    require_openai_compat_api_key,
)


def test_openai_compat_api_key_is_accepted():
    assert asyncio.run(require_openai_compat_api_key(OPENAI_COMPAT_API_KEY)) == OPENAI_COMPAT_API_KEY


def test_invalid_openai_compat_api_key_is_rejected():
    with pytest.raises(HTTPException) as exc:
        asyncio.run(require_openai_compat_api_key("wrong"))
    assert exc.value.status_code == 401
