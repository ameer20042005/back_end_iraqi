# -*- coding: utf-8 -*-
"""باك اند البيانات Catalog وأدواته — بـ httpx.MockTransport بلا شبكة.

  ١. العميل: الهيدرات (Bearer + X-Tenant-Id)، ترميز "+"، PageResponse، الأخطاء الموحّدة.
  ٢. أدوات المكالمة: رقم الزبون من metadata الموثوقة، وفحص ملكية الطلب.
  ٣. الإعدادات: بلا CATALOG_BASE_URL الأدوات ما تنعرض والتعليمات تگول "ما أگدر أتأكد".
  ٤. مساعد الموظف: أدوات الخادم تتنفّذ بحلقة وما توصل للتطبيق.
  ٥. إرسال النتيجة: مسارات قابلة للتعطيل.
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.core import catalog
from app.features.voice_followup import call_tools, gateway, operator, operator_tools
from app.features.voice_followup.prompts import START_CALL_TOOL_NAME, build_call_instructions
from app.features.voice_followup.schema import AgentCallMetadata, CallOutcome

BASE = "https://catalog.test"
PHONE = "07701234567"
MY_ORDER = {"id": 41, "customerName": "أمير", "customerPhoneNumber": "+9647701234567", "sellStatus": "SUCCESS",
            "currentStep": {"code": "SHIPPED", "nameAr": "تم الشحن"}, "totalNetAmount": 25000,
            "deliveryAddress": "الكرادة", "createdAt": "2026-10-10T10:00:00Z",
            "items": [{"productName": "غسالة", "variantTitle": "8 كغم", "quantity": 1, "unitNetPrice": 25000}]}
OTHER_ORDER = {"id": 42, "customerName": "غيره", "customerPhoneNumber": "07801234567", "sellStatus": "PENDING"}
# فلتر Catalog جزئي: يرجّع رقم مشابه لزبون ثاني.
LOOKALIKE = {"id": 43, "customerName": "شبيه", "customerPhoneNumber": "0770123456799", "sellStatus": "DRAFT"}


def page(content, number=0, pages=1):
    return {"content": content, "totalElements": len(content), "totalPages": pages, "number": number, "size": 10}


class Backend:
    def __init__(self):
        self.requests = []
        self.fail = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail:
            return httpx.Response(self.fail)
        path, q = request.url.path, request.url.params
        if path == "/api/catalog/v1/online/sells/all":
            return httpx.Response(200, json=page([MY_ORDER, LOOKALIKE, OTHER_ORDER]))
        if path == "/api/catalog/v1/online/sells/41":
            return httpx.Response(200, json=MY_ORDER)
        if path == "/api/catalog/v1/online/sells/42":
            return httpx.Response(200, json=OTHER_ORDER)
        if path.startswith("/api/catalog/v1/online/sells/"):
            return httpx.Response(404, json={"message": "not found"})
        if path == "/api/catalog/v1/products/search":
            return httpx.Response(200, json=page([{"id": 7, "name": "غسالة LG", "categoryName": "أجهزة", "sku": "x"},
                                                  {"id": 8, "name": "غسالة سامسونج"}]))
        if path == "/api/catalog/v1/stock-infos/product/7":
            if q["page"] == "0":
                return httpx.Response(200, json=page([
                    {"stockInfoId": 1, "productId": 7, "variantTitle": "8 كغم", "stockQuantity": 3,
                     "regularActivePrice": 250000, "storageId": 5},
                    {"stockInfoId": 2, "productId": 7, "variantTitle": "10 كغم", "stockQuantity": 0,
                     "regularActivePrice": 300000, "storageId": 9}], number=0, pages=2))
            return httpx.Response(200, json=page([
                {"stockInfoId": 3, "productId": 7, "variantTitle": "12 كغم", "stockQuantity": 1,
                 "regularActivePrice": 350000, "storageId": 5}], number=1, pages=2))
        if path == "/api/catalog/v1/stock-infos/product/8":
            return httpx.Response(503)
        if path == "/api/catalog/v1/categories/all":
            return httpx.Response(200, json=[{"id": 1, "name": "أجهزة"}])
        if path == "/outcomes":
            return httpx.Response(201)
        return httpx.Response(500)


@pytest.fixture
def backend(monkeypatch):
    configured = replace(catalog.settings, catalog_base_url=BASE, catalog_access_token="jwt-token",
                         catalog_tenant_id="tenant-7", catalog_storage_id="5")
    monkeypatch.setattr(catalog, "settings", configured)
    server = Backend()
    monkeypatch.setattr(catalog, "_client", httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(server)))
    return server


def run(coro):
    return asyncio.run(coro)


def ctx(phone=PHONE):
    return call_tools.CallToolContext(
        room="call-t-1", meta=AgentCallMetadata(company_name="X", goal="g", phone=phone))


# ── ١. العميل ───────────────────────────────────────────────────────────────

def test_headers_phone_encoding_and_page_params(backend):
    run(catalog.get_customer_orders("+9647701234567"))
    req = backend.requests[0]
    assert req.headers["Authorization"] == "Bearer jwt-token"
    assert req.headers["X-Tenant-Id"] == "tenant-7"
    assert "customerPhoneNumber=%2B9647701234567" in str(req.url)
    assert req.url.params["page"] == "0" and req.url.params["size"] == "10"


def test_customer_orders_keep_only_the_same_number(backend):
    orders = run(catalog.get_customer_orders(PHONE))
    assert [o["id"] for o in orders] == [41]


def test_errors_are_unified(backend):
    backend.fail = 401
    with pytest.raises(catalog.CatalogError) as err:
        run(catalog.get_order(41))
    assert err.value.reason == "unauthorized" and err.value.status == 401
    backend.fail = 502
    with pytest.raises(catalog.CatalogError) as err:
        run(catalog.search_products("x"))
    assert err.value.reason == "unavailable"


def test_network_error_and_not_configured(backend, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(catalog, "_client", httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(boom)))
    with pytest.raises(catalog.CatalogError) as err:
        run(catalog.get_categories())
    assert err.value.reason == "unavailable"
    monkeypatch.setattr(catalog, "settings", replace(catalog.settings, catalog_base_url=""))
    with pytest.raises(catalog.CatalogError) as err:
        run(catalog.get_categories())
    assert err.value.reason == "not_configured"


@pytest.mark.parametrize("bad", ["1/../admin", "-3", "abc", "0"])
def test_path_ids_are_validated(backend, bad):
    with pytest.raises(catalog.CatalogError) as err:
        run(catalog.get_order(bad))
    assert err.value.reason == "bad_request"
    assert backend.requests == []


def test_stock_pages_storage_filter_and_trimming(backend):
    products = run(catalog.search_products_with_stock("غسالة"))
    lg, samsung = products
    assert lg["product_id"] == 7 and lg["in_stock"] is True
    assert lg["price"] == {"from": 250000, "to": 350000}  # مخزن 5 بس (300000 بمخزن 9 مستبعد)
    assert {v["variant"] for v in lg["variants"]} == {"8 كغم", "12 كغم"}
    assert "sku" not in lg
    assert samsung == {"product_id": 8, "name": "غسالة سامسونج"}  # فشل المخزون ما يفشّل البحث


def test_same_phone_uses_last_ten_digits():
    assert catalog.same_phone("07701234567", "+964 770 123 4567")
    assert not catalog.same_phone("07701234567", "07801234567")
    assert not catalog.same_phone("", "")


# ── ٢. أدوات المكالمة ───────────────────────────────────────────────────────

def test_get_my_orders_uses_trusted_phone_not_model_args(backend):
    result = run(call_tools.run_tool(ctx(), "get_my_orders", {"phone": "07801234567"}))
    data = json.loads(result)
    assert data["count"] == 1 and data["orders"][0]["order_id"] == 41
    assert data["orders"][0]["step"] == "تم الشحن"
    assert "customerName" not in result and "أمير" not in result and "الكرادة" not in result
    assert backend.requests[0].url.params["customerPhoneNumber"] == PHONE


def test_get_my_orders_without_phone_does_not_call_backend(backend):
    result = run(call_tools.run_tool(ctx(phone=None), "get_my_orders", {}))
    assert "غير معروف" in result and backend.requests == []


def test_get_order_status_checks_ownership(backend):
    own = json.loads(run(call_tools.run_tool(ctx(), "get_order_status", {"order_id": "41"})))
    assert own["order_id"] == 41 and own["status"] == "SUCCESS"
    assert run(call_tools.run_tool(ctx(), "get_order_status", {"order_id": "42"})) == call_tools.NOT_FOUND_ORDER
    assert run(call_tools.run_tool(ctx(), "get_order_status", {"order_id": "999"})) == call_tools.NOT_FOUND_ORDER
    assert run(call_tools.run_tool(ctx(), "get_order_status", {"order_id": "41 OR 1=1"})) == call_tools.NOT_FOUND_ORDER


def test_backend_outage_becomes_a_spoken_apology(backend):
    backend.fail = 503
    result = run(call_tools.run_tool(ctx(), "get_order_status", {"order_id": "41"}))
    assert "الفريق" in result


def test_search_products_tool(backend):
    data = json.loads(run(call_tools.run_tool(ctx(), "search_products", {"text": "غسالة"})))
    assert data["count"] == 2 and data["products"][0]["name"] == "غسالة LG"


# ── ٣. الإعدادات ────────────────────────────────────────────────────────────

def test_data_tools_hidden_without_catalog(monkeypatch):
    monkeypatch.setattr(catalog, "settings", replace(catalog.settings, catalog_base_url=""))
    names = {t["function"]["name"] for t in call_tools.openai_tools()}
    assert names == {"save_outcome", "end_call"}
    assert not call_tools.data_tools_enabled()
    assert operator_tools.active_tools() == []
    assert "غير موجودة" in run(call_tools.run_tool(ctx(), "get_my_orders", {}))
    meta = AgentCallMetadata(company_name="X", goal="g")
    assert "لا تملك في هذه المكالمة وصولاً" in build_call_instructions(meta, call_tools.data_tools_enabled())


def test_data_tools_offered_with_catalog(backend):
    names = {t["function"]["name"] for t in call_tools.openai_tools()}
    assert {"get_my_orders", "get_order_status", "search_products"} <= names
    # رقم الهاتف ما يكون وسيطاً بمخطط أي أداة مكالمة.
    for tool in call_tools.openai_tools():
        assert "phone" not in tool["function"]["parameters"].get("properties", {})
    text = build_call_instructions(AgentCallMetadata(company_name="X", goal="g", phone=PHONE), True)
    assert "get_my_orders" in text and PHONE not in text


# ── ٤. مساعد الموظف ─────────────────────────────────────────────────────────

class FakeEngine:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def create_chat_completion(self, messages, **kwargs):
        self.requests.append((json.loads(json.dumps(messages)), kwargs))
        return self.responses.pop(0)


def tool_call(name, arguments, call_id="call_1"):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}


def text(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _req(**kw):
    return operator.OperatorChatRequest(model="operator", company_name="متجر النور",
                                        messages=[{"role": "user", "content": "اتصل بصاحب الطلب 41"}], **kw)


def test_operator_runs_server_tools_and_returns_only_app_calls(backend):
    engine = FakeEngine(
        tool_call("search_orders", {"order_id": "41"}),
        tool_call(START_CALL_TOOL_NAME, {"goal": "تابع الطلب", "contact": {"phone": PHONE}}, "call_2"),
    )
    response = run(operator.run_operator_turn(engine, _req()))
    calls = response["choices"][0]["message"]["tool_calls"]
    assert [c["function"]["name"] for c in calls] == [START_CALL_TOOL_NAME]
    second_messages = engine.requests[1][0]
    assert second_messages[-1]["role"] == "tool" and '"order_id":41' in second_messages[-1]["content"]
    assert "أمير" in second_messages[-1]["content"]  # الموظف يشوف اسم الزبون ورقمه
    tool_names = {t["function"]["name"] for t in engine.requests[0][1]["tools"]}
    assert {"search_orders", "search_products", START_CALL_TOOL_NAME} <= tool_names


def test_operator_server_tool_then_text(backend):
    engine = FakeEngine(tool_call("search_products", {"text": "غسالة"}), text("عندنا غسالة LG متوفرة."))
    response = run(operator.run_operator_turn(engine, _req()))
    message = response["choices"][0]["message"]
    assert message == {"role": "assistant", "content": "عندنا غسالة LG متوفرة."}
    assert response["choices"][0]["finish_reason"] == "stop"


def test_operator_server_tool_loop_is_bounded(backend):
    loops = [tool_call("search_products", {"text": "x"}, f"c{i}") for i in range(operator.MAX_SERVER_TOOL_ROUNDS)]
    engine = FakeEngine(*loops, text("ما لگيت."))
    response = run(operator.run_operator_turn(engine, _req()))
    assert response["choices"][0]["message"]["content"] == "ما لگيت."
    last_tools = {t["function"]["name"] for t in engine.requests[-1][1]["tools"]}
    assert "search_products" not in last_tools


def test_operator_search_orders_filters(backend):
    result = run(operator_tools.run("search_orders", {"phone": PHONE, "created_at": "2026-10-10"}))
    data = json.loads(result)
    assert [o["order_id"] for o in data["orders"]] == [41]
    params = backend.requests[0].url.params
    assert params["customerPhoneNumber"] == PHONE and params["createdAt"] == "2026-10-10"
    assert "YYYY-MM-DD" in run(operator_tools.run("search_orders", {"created_at": "10/10/2026"}))
    assert "فلتر" in run(operator_tools.run("search_orders", {}))


def test_operator_app_tool_with_server_name_is_replaced(backend):
    app_tool = operator.OperatorTool.model_validate(
        {"type": "function", "function": {"name": "search_products", "parameters": {"type": "object"}}})
    tools = operator.build_tools([app_tool], operator_tools.active_tools())
    assert [t["function"]["name"] for t in tools].count("search_products") == 1


# ── ٥. إرسال النتيجة ────────────────────────────────────────────────────────

def test_outcome_forwarding_paths_are_configurable(backend, monkeypatch):
    outcome = CallOutcome(room="call-1", status="done", summary="تمام")
    assert run(gateway.CatalogCallOutcomeSubmitter().submit(outcome, "k")) is False  # المسار فارغ
    monkeypatch.setattr(catalog, "settings", replace(catalog.settings, catalog_path_call_outcome="/outcomes"))
    assert run(gateway.CatalogCallOutcomeSubmitter().submit(outcome, "k")) is True
    assert json.loads(backend.requests[-1].content)["summary"] == "تمام"

    monkeypatch.setattr(gateway, "settings", replace(gateway.settings, system_backend_call_outcome_path=""))
    assert run(gateway.HttpCallOutcomeSubmitter().submit(outcome, "k")) is False


def test_composite_submitter_tries_all_then_raises():
    class Boom(gateway.CallOutcomeSubmitter):
        async def submit(self, outcome, api_key):
            raise RuntimeError("down")

    class Ok(gateway.CallOutcomeSubmitter):
        called = False

        async def submit(self, outcome, api_key):
            Ok.called = True
            return True

    with pytest.raises(RuntimeError):
        run(gateway.CompositeCallOutcomeSubmitter(Boom(), Ok()).submit(CallOutcome(room="r", status="done"), "k"))
    assert Ok.called
