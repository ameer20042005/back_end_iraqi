# -*- coding: utf-8 -*-
"""طبقة الاتصال بباك اند السستم (app/system_backend.py — docs/fix-plan.md
§ المرحلة 7): التقاط 4xx كـSystemBackendUnavailable (العطل A5)، تمرير 404
كنتيجة مشروعة، والعميل المشترك (العطل B6) مع ترويسات لكل طلب.

بلا شبكة: httpx.MockTransport يرد بما نريده.

التشغيل:  python -m pytest tests/test_system_backend.py -v
"""

import asyncio

import httpx
import pytest

from app import system_backend
from app.order_gateway import HttpOrderStatusProvider
from app.system_backend import SystemBackendUnavailable, auth_headers, caller_auth_token


def _client(status_code, body=b"{}"):
    def handler(request):
        return httpx.Response(status_code, content=body, request=request)
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://backend.test")


def _request(client):
    async def go():
        async with client:
            return await system_backend.request(client, "GET", "/orders")
    return asyncio.run(go())


def test_401_becomes_system_backend_unavailable():
    """مفتاح API خاطئ → رسالة عربية مفهومة، لا HTTPStatusError خام → 500."""
    with pytest.raises(SystemBackendUnavailable) as exc:
        _request(_client(401))
    assert "401" in str(exc.value)


def test_503_becomes_system_backend_unavailable():
    with pytest.raises(SystemBackendUnavailable):
        _request(_client(503))


def test_404_is_returned_not_raised():
    resp = _request(_client(404))
    assert resp.status_code == 404


def test_200_is_returned():
    resp = _request(_client(200, b'{"orders": []}'))
    assert resp.json() == {"orders": []}


def test_connection_error_becomes_system_backend_unavailable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://backend.test")
    with pytest.raises(SystemBackendUnavailable):
        _request(client)


def test_provider_sends_auth_per_request_on_shared_client(monkeypatch):
    """توكن المستخدم يختلف بين طلب وآخر؛ لازم يُرسل بترويسات **الطلب** لا
    على العميل المشترك — وإلا صار كل المستخدمين على شركة واحدة."""
    seen = []

    def handler(request):
        seen.append((str(request.url), request.headers.get("Authorization"), request.headers.get("X-API-Key")))
        return httpx.Response(200, content=b'{"orders": []}', request=request)

    shared = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(system_backend, "_client", shared)
    provider = HttpOrderStatusProvider(base_url="http://backend.test/internal")

    async def go():
        caller_auth_token.set("jwt-A")
        await provider.list_all("svc-key")
        caller_auth_token.set("jwt-B")
        await provider.list_all("svc-key")
        await shared.aclose()

    asyncio.run(go())
    assert seen == [
        ("http://backend.test/internal/orders", "Bearer jwt-A", "svc-key"),
        ("http://backend.test/internal/orders", "Bearer jwt-B", "svc-key"),
    ]


def test_auth_headers_without_token_has_only_api_key():
    caller_auth_token.set(None)
    assert auth_headers("k") == {"X-API-Key": "k"}


def test_get_client_recreates_after_close():
    async def go():
        first = system_backend.get_client()
        await system_backend.close_client()
        second = system_backend.get_client()
        await system_backend.close_client()
        return first is not second

    assert asyncio.run(go()) is True
