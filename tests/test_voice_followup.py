# -*- coding: utf-8 -*-
"""المكالمات الحية (LiveKit) ومساعد الموظف: كل ما هو حتمي قابل للفحص بلا موديل.

  ١. الخصوصية: اسم الزبون ورقمه ما يدخلان سياق نموذج المكالمة.
  ٢. مواعيد التأجيل: السقف مفروض بالجدول، والتاريخ يُحسب بالكود.
  ٣. أدوات الـ agent: ترفض المفاتيح الشاذة، وما تحفظ موعداً بلا اقتراح مسبق.
  ٤. مساعد الموظف: start_call تُضاف دائماً، وأسماء الأدوات المجهولة تُرفض.
  ٥. نقطة /calls: 503 واضحة بلا إعداد LiveKit، وdispatch بالـ metadata الصحيحة.
"""

import asyncio
import json
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient

# الـ agent worker تبعيته بـ requirements-gpu.txt؛ بدونه تُتخطى هذه الاختبارات.
pytest.importorskip("livekit.agents")
from livekit.agents import ToolError  # noqa: E402

from app import main  # noqa: E402
from app.features.voice_followup import agent as agent_module
from app.features.voice_followup import calls, operator
from app.features.voice_followup.postpone import (
    MAX_POSTPONE_DAYS,
    POSTPONE_CHOICES,
    option_label,
    postpone_days,
    resolve_postpone_date,
)
from app.features.voice_followup.prompts import (
    START_CALL_TOOL_NAME,
    build_followup_instructions,
    build_postpone_instructions,
)
from app.features.voice_followup.schema import CallRequest, VoiceFollowupOrderRequest

ORDER = VoiceFollowupOrderRequest(
    order_id="ORD-1001",
    status="قيد التوصيل",
    customer_name="أمير وسام",
    customer_phone="07701234567",
    items=[{"product_name": "غسالة اتوماتيك", "quantity": 1}],
    scheduled_date="2026-09-05",
)
VOICE_KEY = {"X-API-Key": main.settings.voice_followup_api_key}


# ── ١. الخصوصية ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("build", [build_followup_instructions, build_postpone_instructions])
def test_call_instructions_never_receive_customer_name_or_phone(build):
    text = build(ORDER)
    assert "أمير" not in text and "وسام" not in text
    assert "07701234567" not in text
    assert "غسالة اتوماتيك" in text


def test_postpone_instructions_hide_order_number():
    assert "ORD-1001" not in build_postpone_instructions(ORDER)


# ── ٢. مواعيد التأجيل ───────────────────────────────────────────────────────

def test_choices_are_generated_from_the_cap():
    assert f"plus_{MAX_POSTPONE_DAYS}" in POSTPONE_CHOICES
    assert f"plus_{MAX_POSTPONE_DAYS + 1}" not in POSTPONE_CHOICES
    assert len(POSTPONE_CHOICES) == 1 + MAX_POSTPONE_DAYS + 7


def test_every_choice_is_computable_within_the_cap():
    for choice in POSTPONE_CHOICES:
        assert 0 <= postpone_days(choice, date(2026, 9, 1)) <= MAX_POSTPONE_DAYS


def test_resolve_postpone_date_offsets():
    today = date(2026, 9, 1)  # ثلاثاء
    assert resolve_postpone_date("today", today) == "2026-09-01"
    assert resolve_postpone_date("plus_1", today) == "2026-09-02"
    assert resolve_postpone_date("plus_14", today) == "2026-09-15"
    # نفس اليوم بالأسبوع = الأسبوع الجاي، مو اليوم.
    assert resolve_postpone_date("weekday_1", today) == "2026-09-08"
    assert resolve_postpone_date("weekday_3", today) == "2026-09-03"


def test_option_label_echoes_customer_phrasing():
    assert option_label("plus_1", 1) == "باجر"
    assert option_label("plus_2", 2) == "بعد باجر"
    assert option_label("weekday_3", 2) == "يوم الخميس"
    assert option_label("plus_4", 4) == "بعد 4 أيام"


# ── ٣. أدوات الـ agent ──────────────────────────────────────────────────────

class RecordingSubmitter:
    def __init__(self):
        self.calls = []

    async def submit(self, *args):
        self.calls.append(args)
        return True


def test_postpone_tool_schema_is_limited_to_the_choice_table():
    from livekit.agents.llm import utils

    agent = agent_module.PostponeAgent(ORDER)
    tool = next(t for t in agent.tools if getattr(t, "id", "") == "propose_postpone")
    schema = utils.build_legacy_openai_schema(tool)
    assert schema["function"]["parameters"]["properties"]["choice"]["enum"] == list(POSTPONE_CHOICES)


def test_confirm_without_proposal_is_rejected(monkeypatch):
    submitter = RecordingSubmitter()
    monkeypatch.setattr(agent_module, "voice_postpone_submitter", submitter)
    agent = agent_module.PostponeAgent(ORDER)
    with pytest.raises(ToolError):
        asyncio.run(agent.confirm_postpone(None))
    assert submitter.calls == []


def test_choice_outside_the_table_is_rejected():
    agent = agent_module.PostponeAgent(ORDER)
    with pytest.raises(ToolError):
        asyncio.run(agent.propose_postpone(None, "plus_30"))


def test_proposal_then_confirmation_saves_computed_date(monkeypatch):
    submitter = RecordingSubmitter()
    monkeypatch.setattr(agent_module, "voice_postpone_submitter", submitter)
    agent = agent_module.PostponeAgent(ORDER)

    reply = asyncio.run(agent.propose_postpone(None, "plus_2"))
    assert "بعد باجر" in reply
    asyncio.run(agent.confirm_postpone(None))
    asyncio.run(agent.confirm_postpone(None))  # تأكيد مكرر ما يرسل مرتين

    assert len(submitter.calls) == 1
    order, new_date, choice, _key = submitter.calls[0]
    assert order.order_id == "ORD-1001"
    assert choice == "plus_2"
    assert new_date == resolve_postpone_date("plus_2")


def test_save_reason_submits_summary_and_customer_words(monkeypatch):
    submitter = RecordingSubmitter()
    monkeypatch.setattr(agent_module, "voice_followup_submitter", submitter)
    agent = agent_module.FollowupAgent(ORDER)

    class Item:
        def __init__(self, role, text):
            self.role, self.text_content = role, text

    class Session:
        history = type("H", (), {"items": [Item("assistant", "شنو السبب؟"), Item("user", "تأخر التوصيل")]})()

    context = type("Ctx", (), {"session": Session()})()
    asyncio.run(agent.save_reason(context, "الزبون ألغى لأن التوصيل تأخر"))
    asyncio.run(agent.save_reason(context, "مكرر"))

    assert len(submitter.calls) == 1
    _order, summary, transcript, _key = submitter.calls[0]
    assert summary == "الزبون ألغى لأن التوصيل تأخر"
    assert transcript == "تأخر التوصيل"


def test_call_type_selects_the_agent():
    assert isinstance(agent_module.build_agent(CallRequest(call_type="postpone", order=ORDER)),
                      agent_module.PostponeAgent)
    assert isinstance(agent_module.build_agent(CallRequest(call_type="followup", order=ORDER)),
                      agent_module.FollowupAgent)


# ── ٤. مساعد الموظف ─────────────────────────────────────────────────────────

class FakeEngine:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    async def create_chat_completion(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        return self.responses.pop(0)


def _tool_call(name, arguments):
    return {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}]}


SEARCH_TOOL = {"type": "function", "function": {"name": "search_orders", "parameters": {"type": "object"}}}


def test_start_call_tool_is_always_added_and_overrides_client_definition():
    tools = operator.build_tools([
        operator.OperatorTool.model_validate(SEARCH_TOOL),
        operator.OperatorTool.model_validate({"type": "function", "function": {"name": START_CALL_TOOL_NAME}}),
    ])
    names = [t["function"]["name"] for t in tools]
    assert names == ["search_orders", START_CALL_TOOL_NAME]
    assert "call_type" in tools[-1]["function"]["parameters"]["properties"]


def test_operator_turn_returns_start_call_arguments_usable_as_call_request():
    args = {"call_type": "followup", "order": {"order_id": "ORD-7", "status": "ملغي", "customer_phone": "0770"}}
    engine = FakeEngine(_tool_call(START_CALL_TOOL_NAME, args))
    req = operator.OperatorChatRequest(model="operator", messages=[{"role": "user", "content": "اتصل بصاحب الطلب ORD-7"}],
                                       tools=[SEARCH_TOOL])
    response = asyncio.run(operator.run_operator_turn(engine, req))

    call = response["choices"][0]["message"]["tool_calls"][0]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    assert CallRequest.model_validate_json(call["function"]["arguments"]).order.order_id == "ORD-7"
    system = engine.requests[0][0][0]
    assert system["role"] == "system" and "start_call" in system["content"]


def test_operator_rejects_unknown_tool_and_falls_back():
    engine = FakeEngine(_tool_call("delete_everything", {}), _tool_call("delete_everything", {}))
    req = operator.OperatorChatRequest(model="operator", messages=[{"role": "user", "content": "هلا"}])
    response = asyncio.run(operator.run_operator_turn(engine, req))
    assert response["choices"][0]["message"]["content"] == operator.FALLBACK_REPLY


def test_operator_cleans_leaked_template_channels():
    assert operator.clean_content("<|channel>thought\n<channel|>بدأت مكالمتين") == "بدأت مكالمتين"


# ── ٥. نقطة /calls ──────────────────────────────────────────────────────────

def test_calls_endpoint_reports_missing_livekit_config(monkeypatch):
    monkeypatch.setattr(calls, "settings", replace(calls.settings, livekit_url="", livekit_api_key="", livekit_api_secret=""))
    client = TestClient(main.app)
    res = client.post("/voice_followup/calls", headers=VOICE_KEY,
                      json={"call_type": "followup", "order": {"order_id": "1", "status": "ملغي"}})
    assert res.status_code == 503


def test_calls_endpoint_requires_voice_key():
    client = TestClient(main.app)
    res = client.post("/voice_followup/calls", headers={"X-API-Key": "wrong"},
                      json={"call_type": "followup", "order": {"order_id": "1", "status": "ملغي"}})
    assert res.status_code == 401


def test_start_call_creates_room_and_dispatches_agent_with_order(monkeypatch):
    configured = replace(calls.settings, livekit_url="wss://lk.example", livekit_api_key="APIkey",
                         livekit_api_secret="secret" * 6, livekit_sip_trunk_id="ST_1")
    monkeypatch.setattr(calls, "settings", configured)
    recorded = {}

    class FakeRoom:
        async def create_room(self, req):
            recorded["room"] = req

    class FakeDispatch:
        async def create_dispatch(self, req):
            recorded["dispatch"] = req
            return type("D", (), {"id": "AD_1"})()

    class FakeAPI:
        def __init__(self, *args):
            self.room, self.agent_dispatch = FakeRoom(), FakeDispatch()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(calls.api, "LiveKitAPI", FakeAPI)
    response = asyncio.run(calls.start_call(CallRequest(call_type="postpone", order=ORDER)))

    assert response.room == recorded["room"].name == recorded["dispatch"].room
    assert recorded["dispatch"].agent_name == configured.livekit_agent_name
    assert CallRequest.model_validate_json(recorded["dispatch"].metadata).order.order_id == "ORD-1001"
    assert response.dial_mode == "sip"
    assert response.dispatch_id == "AD_1"
    assert response.operator_token.count(".") == 2  # JWT


# ── ٦. مجمّع نسخ النطق (التزامن) ────────────────────────────────────────────

class FakeVoice:
    sr = 24000
    created = 0

    def __init__(self):
        FakeVoice.created += 1
        self.prepared = []
        self.generated = []

    @classmethod
    def from_pretrained(cls, **kwargs):
        return cls()

    def prepare_conditionals(self, path, exaggeration):
        self.prepared.append(path)

    def generate(self, text, **kwargs):
        import numpy as np

        self.generated.append(kwargs)
        return type("T", (), {"squeeze": lambda s, i: type("U", (), {
            "detach": lambda u: type("V", (), {"cpu": lambda v: type("W", (), {
                "numpy": lambda w: np.zeros(10, dtype="float32")})()})()})()})()


def _fake_pool(monkeypatch, size):
    import sys
    from types import SimpleNamespace

    from app.features.voice_followup import tts

    FakeVoice.created = 0
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setattr(tts, "ChatterboxMultilingualTTS", FakeVoice, raising=False)
    monkeypatch.setattr(tts, "_CHATTERBOX_AVAILABLE", True)
    monkeypatch.setattr(tts, "settings", replace(tts.settings, tts_pool_size=size))
    monkeypatch.setattr(tts, "_pool", None)
    return tts


def test_voice_fingerprint_is_prepared_once_per_instance(monkeypatch):
    tts = _fake_pool(monkeypatch, size=1)
    for _ in range(3):
        audio, sr = tts.synthesize_array("هلا بيك")
    inst = tts._get_pool()._free[0]
    assert FakeVoice.created == 1
    assert len(inst.prepared) == 1 and inst.prepared[0].endswith("mazen.mp3")
    # بلا audio_prompt_path بكل جملة — البصمة محسوبة مسبقاً.
    assert all("audio_prompt_path" not in kw for kw in inst.generated)
    assert sr == 24000


def test_pool_grows_to_size_then_waits_for_a_free_instance(monkeypatch):
    import threading

    tts = _fake_pool(monkeypatch, size=2)
    pool = tts._get_pool()
    first, second = pool.acquire(), pool.acquire()
    assert first is not second and FakeVoice.created == 2

    got = []
    waiter = threading.Thread(target=lambda: got.append(pool.acquire()))
    waiter.start()
    waiter.join(timeout=0.2)
    assert waiter.is_alive() and FakeVoice.created == 2  # ما تنشئ نسخة ثالثة

    pool.release(first)
    waiter.join(timeout=2)
    assert got == [first]


def test_worker_load_counts_calls_against_the_cap(monkeypatch):
    monkeypatch.setattr(agent_module, "settings", replace(agent_module.settings, voice_max_concurrent_calls=10))
    server = type("S", (), {"active_jobs": [object()] * 9})()
    assert agent_module._call_load(server) == 0.9 < agent_module.server._load_threshold
    server.active_jobs = [object()] * 10
    assert agent_module._call_load(server) >= agent_module.server._load_threshold
