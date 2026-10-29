# -*- coding: utf-8 -*-
"""المكالمات الحية العامة (LiveKit) ومساعد الموظف: كل ما هو حتمي قابل للفحص بلا موديل.

  ١. العقد: CallRequest يقبل الشكل الجديد ويرفض الناقص/الزائد عن الحدود.
  ٢. الخصوصية: اسم الزبون ورقمه ما يدخلان metadata الـ agent ولا تعليماته.
  ٣. التعليمات: عامة بلا شخصيات ثابتة، باسم الشركة وهدف الموظف.
  ٤. أداة save_outcome: تحفظ مرة وحدة، وترسل لباك اند السستم best effort.
  ٥. مخزن النتائج: ملفات مشتركة، pending، TTL، ورفض path traversal.
  ٦. مساعد الموظف: start_call تُضاف دائماً، company_name بالـ system، والأسماء المجهولة تُرفض.
  ٧. نقطة /calls: 503 واضحة بلا إعداد LiveKit، وdispatch بالـ metadata الصحيحة.
"""

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import main  # noqa: E402
from app.features.voice_followup import call_tools as agent_module, gateway  # noqa: E402
from app.features.voice_followup import calls, operator, outcomes  # noqa: E402
from app.features.voice_followup.prompts import (  # noqa: E402
    START_CALL_TOOL,
    START_CALL_TOOL_NAME,
    build_call_instructions,
    build_operator_system_prompt,
)
from app.features.voice_followup.schema import (  # noqa: E402
    AgentCallMetadata,
    CallOutcome,
    CallRequest,
    Contact,
    OutcomeAnswer,
)

REQUEST = CallRequest(
    company_name="متجر النور",
    agent_name="سارة",
    goal="اسأل الزبون عن رأيه بالغسالة وهل يحتاج شي ثاني",
    questions=["هل وصلك الطلب؟", "شنو تقييمك للمنتج؟"],
    context="المنتج: غسالة اتوماتيك. الزبون أمير وسام اشترى قبل أسبوع.",
    contact={"name": "أمير وسام", "phone": "07701234567"},
    reference_id="ORD-1042",
)
VOICE_KEY = {"X-API-Key": main.settings.voice_followup_api_key}


@pytest.fixture
def outcomes_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(outcomes, "settings", replace(outcomes.settings, call_outcomes_dir=tmp_path))
    return tmp_path


# ── ١. العقد ────────────────────────────────────────────────────────────────

def test_call_request_minimal_body_defaults():
    req = CallRequest(company_name="X", goal="تابع الزبون")
    assert req.language == "ar" and req.questions == [] and req.contact.phone is None


@pytest.mark.parametrize("body", [
    {"goal": "g"},                                         # بلا company_name
    {"company_name": "X"},                                 # بلا goal
    {"company_name": "  ", "goal": "g"},                   # اسم فارغ
    {"company_name": "X" * 121, "goal": "g"},
    {"company_name": "X", "goal": "g", "questions": ["q"] * 11},
    {"company_name": "X", "goal": "g", "context": "c" * 2001},
])
def test_call_request_rejects_invalid_bodies(body):
    with pytest.raises(ValidationError):
        CallRequest.model_validate(body)


# ── ٢. الخصوصية ─────────────────────────────────────────────────────────────

def test_agent_metadata_drops_contact_name_and_redacts_free_text():
    meta = calls.agent_metadata(REQUEST)
    dumped = meta.model_dump_json()
    assert "أمير وسام" not in dumped
    assert meta.phone == "07701234567"  # للـ SIP فقط
    assert meta.reference_id == "ORD-1042" and meta.company_name == "متجر النور"


def test_call_instructions_never_receive_customer_name_or_phone():
    text = build_call_instructions(calls.agent_metadata(REQUEST))
    assert "أمير" not in text and "وسام" not in text
    assert "07701234567" not in text
    assert "غسالة اتوماتيك" in text


# ── ٣. التعليمات ────────────────────────────────────────────────────────────

def test_call_instructions_are_generic_and_use_operator_goal():
    text = build_call_instructions(calls.agent_metadata(REQUEST))
    assert "متجر النور" in text and "سارة" in text
    assert REQUEST.goal in text
    assert "1. هل وصلك الطلب؟" in text and "2. شنو تقييمك للمنتج؟" in text
    for persona in ("DATUM", "جيني", '"صباح"'):
        assert persona not in text
    assert "save_outcome" in text and "end_call" in text


def test_call_instructions_without_agent_name_introduce_customer_service():
    text = build_call_instructions(AgentCallMetadata(company_name="شركة س", goal="g"))
    assert "معك خدمة العملاء في شركة س" in text


def test_english_language_switches_the_opening():
    text = build_call_instructions(AgentCallMetadata(company_name="X", goal="g", language="en"))
    assert "بإنجليزية" in text


# ── ٤. أداة save_outcome ────────────────────────────────────────────────────

class RecordingSubmitter:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def submit(self, outcome, api_key):
        self.calls.append(outcome)
        if self.fail:
            raise RuntimeError("backend down")
        return True


def _context():
    return None


def _agent():
    ctx = agent_module.CallToolContext(room="call-ORD-1042-abcd1234", meta=calls.agent_metadata(REQUEST),
                                      customer_transcript=lambda: "اي وصل\nعاجبني")

    class Tools:
        async def save_outcome(self, context, summary, answers=None, follow_up_needed=False):
            return await agent_module._save_outcome(ctx, {"summary": summary, "answers": answers,
                                                         "follow_up_needed": follow_up_needed})

    return Tools()


def test_save_outcome_tool_schema():
    tool = next(t for t in agent_module.openai_tools() if t["function"]["name"] == "save_outcome")
    params = tool["function"]["parameters"]
    assert set(params["properties"]) == {"summary", "answers", "follow_up_needed"}
    assert params["required"] == ["summary"]


def test_save_outcome_stores_once_and_forwards(monkeypatch, outcomes_dir):
    submitter = RecordingSubmitter()
    monkeypatch.setattr(gateway, "call_outcome_submitter", submitter)
    agent = _agent()

    asyncio.run(agent.save_outcome(_context(), "الطلب وصل والزبون راضي",
                                   [{"question": "هل وصلك الطلب؟", "answer": "اي وصل"}], False))
    asyncio.run(agent.save_outcome(_context(), "مكرر"))

    assert len(submitter.calls) == 1
    stored = outcomes.get_outcome("call-ORD-1042-abcd1234")
    assert stored.status == "done" and stored.reference_id == "ORD-1042"
    assert stored.summary == "الطلب وصل والزبون راضي"
    assert stored.answers == [OutcomeAnswer(question="هل وصلك الطلب؟", answer="اي وصل")]
    assert stored.customer_transcript == "اي وصل\nعاجبني"
    assert stored.ended_at


def test_backend_failure_keeps_local_outcome(monkeypatch, outcomes_dir):
    monkeypatch.setattr(gateway, "call_outcome_submitter", RecordingSubmitter(fail=True))
    reply = asyncio.run(_agent().save_outcome(_context(), "الزبون يريد يتواصل وياه مندوب", None, True))
    assert "حُفظت" in reply
    assert outcomes.get_outcome("call-ORD-1042-abcd1234").follow_up_needed is True


def test_local_store_failure_is_reported_to_the_model(monkeypatch):
    submitter = RecordingSubmitter()
    monkeypatch.setattr(gateway, "call_outcome_submitter", submitter)

    def broken(_outcome):
        raise OSError("disk full")

    monkeypatch.setattr(agent_module.outcomes, "save_outcome", broken)
    with pytest.raises(agent_module.CallToolError):
        asyncio.run(_agent().save_outcome(_context(), "ملخص"))
    assert submitter.calls == []


def test_empty_summary_is_rejected(outcomes_dir):
    with pytest.raises(agent_module.CallToolError):
        asyncio.run(_agent().save_outcome(_context(), "   "))


def test_fallback_outcome_never_overwrites_saved_one(monkeypatch, outcomes_dir):
    monkeypatch.setattr(gateway, "call_outcome_submitter", RecordingSubmitter())
    meta = calls.agent_metadata(REQUEST)
    agent_module.save_fallback_outcome(meta, "call-x-1", agent_module.NO_ANSWER_SUMMARY)
    assert outcomes.get_outcome("call-x-1").summary == agent_module.NO_ANSWER_SUMMARY

    asyncio.run(_agent().save_outcome(_context(), "نتيجة حقيقية"))
    agent_module.save_fallback_outcome(meta, "call-ORD-1042-abcd1234", agent_module.NO_OUTCOME_SUMMARY)
    assert outcomes.get_outcome("call-ORD-1042-abcd1234").summary == "نتيجة حقيقية"


# ── ٥. مخزن النتائج ─────────────────────────────────────────────────────────

def test_outcome_endpoint_pending_then_done(outcomes_dir):
    client = TestClient(main.app)
    res = client.get("/voice_followup/calls/call-abc-123/outcome", headers=VOICE_KEY)
    assert res.status_code == 200
    assert res.json()["status"] == "pending" and res.json()["room"] == "call-abc-123"

    outcomes.save_outcome(CallOutcome(room="call-abc-123", status="done", reference_id="R1",
                                      summary="تمام", ended_at=outcomes.now_iso()))
    body = client.get("/voice_followup/calls/call-abc-123/outcome", headers=VOICE_KEY).json()
    assert body["status"] == "done" and body["summary"] == "تمام" and body["reference_id"] == "R1"
    assert set(body) == {"room", "status", "reference_id", "summary", "answers", "follow_up_needed",
                         "customer_transcript", "ended_at"}


def test_outcome_endpoint_requires_voice_key(outcomes_dir):
    res = TestClient(main.app).get("/voice_followup/calls/call-1/outcome", headers={"X-API-Key": "wrong"})
    assert res.status_code == 401


@pytest.mark.parametrize("room", ["..", "..%2F..%2Fetc", "a.b", "-lead", "a" * 129, "room name"])
def test_outcome_endpoint_rejects_unsafe_room(room, outcomes_dir):
    res = TestClient(main.app).get(f"/voice_followup/calls/{room}/outcome", headers=VOICE_KEY)
    assert res.status_code in (404, 422)


@pytest.mark.parametrize("room", ["../secret", "..\\secret", "a/b", "", "x.json"])
def test_store_rejects_path_traversal(room, outcomes_dir):
    with pytest.raises(outcomes.InvalidRoom):
        outcomes.get_outcome(room)
    with pytest.raises(outcomes.InvalidRoom):
        outcomes.save_outcome(CallOutcome.model_construct(room=room, status="done"))


def test_expired_outcomes_read_as_pending_and_are_pruned(monkeypatch, outcomes_dir):
    monkeypatch.setattr(outcomes, "settings", replace(outcomes.settings, call_outcomes_ttl_hours=1))
    outcomes.save_outcome(CallOutcome(room="call-old", status="done"))
    old = time.time() - 2 * 3600
    os.utime(outcomes_dir / "call-old.json", (old, old))
    assert outcomes.get_outcome("call-old").status == "pending"

    outcomes.save_outcome(CallOutcome(room="call-new", status="done"))
    assert not (outcomes_dir / "call-old.json").exists()


def test_store_is_bounded(monkeypatch, outcomes_dir):
    monkeypatch.setattr(outcomes, "settings", replace(outcomes.settings, call_outcomes_max_files=3))
    for i in range(5):
        outcomes.save_outcome(CallOutcome(room=f"call-{i}", status="done"))
        stamp = time.time() - (10 - i)
        os.utime(outcomes_dir / f"call-{i}.json", (stamp, stamp))
    outcomes.save_outcome(CallOutcome(room="call-last", status="done"))
    assert len(list(outcomes_dir.glob("*.json"))) == 3
    assert (outcomes_dir / "call-last.json").exists()


def test_generated_room_names_are_valid_store_keys():
    for ref in ("ORD-1042", "طلب ٥/٦", "../../x", None):
        room = calls.room_name(REQUEST.model_copy(update={"reference_id": ref}))
        assert outcomes.ROOM_RE.fullmatch(room)


# ── ٦. مساعد الموظف ─────────────────────────────────────────────────────────

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


SEARCH_TOOL = {"type": "function", "function": {"name": "search_contacts", "parameters": {"type": "object"}}}


def test_start_call_tool_is_always_added_and_overrides_client_definition():
    tools = operator.build_tools([
        operator.OperatorTool.model_validate(SEARCH_TOOL),
        operator.OperatorTool.model_validate({"type": "function", "function": {"name": START_CALL_TOOL_NAME}}),
    ])
    names = [t["function"]["name"] for t in tools]
    assert names == ["search_contacts", START_CALL_TOOL_NAME]
    assert tools[-1] is START_CALL_TOOL


def test_start_call_tool_schema_matches_call_request():
    params = START_CALL_TOOL["function"]["parameters"]
    assert params["required"] == ["goal", "contact"]
    assert "company_name" not in params["required"]
    assert params["properties"]["contact"]["required"] == ["phone"]
    assert set(params["properties"]) == set(CallRequest.model_fields)


def test_operator_prompt_is_generic_and_names_the_company():
    prompt = build_operator_system_prompt("متجر النور")
    assert "متجر النور" in prompt and "{company_name}" not in prompt
    for old in ("followup", "postpone", "DATUM"):
        assert old not in prompt
    assert "{company_name}" not in build_operator_system_prompt(None)


def test_operator_turn_returns_start_call_arguments_usable_as_call_request():
    args = {"goal": "اسأله عن رأيه بالخدمة", "contact": {"name": "أحمد", "phone": "0770"}, "reference_id": "C-7"}
    engine = FakeEngine(_tool_call(START_CALL_TOOL_NAME, args))
    req = operator.OperatorChatRequest(model="operator", company_name="متجر النور",
                                       messages=[{"role": "user", "content": "اتصل بأحمد واسأله عن رأيه"}],
                                       tools=[SEARCH_TOOL])
    response = asyncio.run(operator.run_operator_turn(engine, req))

    call = response["choices"][0]["message"]["tool_calls"][0]
    assert response["choices"][0]["finish_reason"] == "tool_calls"
    # التطبيق يملأ company_name من إعداداته ثم يرسل الجسم لـ /calls.
    body = dict(json.loads(call["function"]["arguments"]), company_name="متجر النور")
    assert CallRequest.model_validate(body).reference_id == "C-7"
    system = engine.requests[0][0][0]
    assert system["role"] == "system" and "start_call" in system["content"] and "متجر النور" in system["content"]


def test_operator_rejects_unknown_tool_and_falls_back():
    engine = FakeEngine(_tool_call("delete_everything", {}), _tool_call("delete_everything", {}))
    req = operator.OperatorChatRequest(model="operator", messages=[{"role": "user", "content": "هلا"}])
    response = asyncio.run(operator.run_operator_turn(engine, req))
    assert response["choices"][0]["message"]["content"] == operator.FALLBACK_REPLY


def test_operator_cleans_leaked_template_channels():
    assert operator.clean_content("<|channel>thought\n<channel|>بدأت مكالمتين") == "بدأت مكالمتين"


# ── ٧. نقطة /calls ──────────────────────────────────────────────────────────

CALL_BODY = {"company_name": "X", "goal": "تابع الزبون", "contact": {"phone": "0770"}}


def test_calls_endpoint_uses_pipecat_without_rtc_config():
    res = TestClient(main.app).post("/voice_followup/calls", headers=VOICE_KEY, json=CALL_BODY)
    assert res.status_code == 200
    assert res.json()["transport"] == "pipecat"
    assert res.json()["stream_path"] == "/voice_followup/stream"


def test_calls_endpoint_requires_voice_key():
    res = TestClient(main.app).post("/voice_followup/calls", headers={"X-API-Key": "wrong"}, json=CALL_BODY)
    assert res.status_code == 401


def test_calls_endpoint_rejects_old_contract():
    res = TestClient(main.app).post("/voice_followup/calls", headers=VOICE_KEY,
                                    json={"call_type": "followup", "order": {"order_id": "1", "status": "ملغي"}})
    assert res.status_code == 422


class FakeVoice:
    created = 0

    def __init__(self, ckpt_file, vocab_file):
        FakeVoice.created += 1
        self.files = (ckpt_file, vocab_file)
        self.generated = []
        self.vocoder = SimpleNamespace(float=lambda: "float32-vocoder")

    def infer(self, **kwargs):
        import numpy as np

        self.generated.append(kwargs)
        return np.zeros(10, dtype="float32"), 24000, None


def _fake_pool(monkeypatch, size):
    import sys

    from app.features.voice_followup import tts

    FakeVoice.created = 0
    monkeypatch.setitem(sys.modules, "huggingface_hub",
                        SimpleNamespace(hf_hub_download=lambda repo_id, filename: filename))
    monkeypatch.setattr(tts, "F5TTS", FakeVoice, raising=False)
    monkeypatch.setattr(tts, "_F5_TTS_AVAILABLE", True)
    monkeypatch.setattr(tts, "settings", replace(tts.settings, tts_pool_size=size))
    monkeypatch.setattr(tts, "_pool", None)
    return tts


def test_voice_loads_once_and_uses_local_reference_with_its_text(monkeypatch):
    tts = _fake_pool(monkeypatch, size=1)
    for _ in range(3):
        audio, sr = tts.synthesize_array("هلا بيك")
    inst = tts._get_pool()._free[0]
    assert FakeVoice.created == 1
    assert inst.files == (tts.settings.tts_ckpt_ar, tts.settings.tts_vocab_ar)
    assert inst.vocoder == "float32-vocoder"
    ref = inst.generated[0]
    assert ref["ref_file"].endswith("MSA.wav") and Path(ref["ref_file"]).is_file()
    assert ref["ref_text"] == tts.normalize_text(tts.settings.tts_ref_text_ar, None) and ref["gen_text"] == "هلا بيك."
    assert sr == 24000 and audio.dtype == "float32"


def test_tts_text_maps_arabic_punctuation_and_drops_unknown_marks():
    from app.features.voice_followup import tts

    vocab = frozenset(" ,.?مرحباهلوصك")
    assert tts.normalize_text("مرحباً، هل وصلك؟", vocab) == "مرحبا, هل وصلك?"


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


def test_pending_pipecat_sessions_are_bounded(monkeypatch):
    monkeypatch.setattr(calls, "_pending", {})
    monkeypatch.setattr(calls, "_MAX_PENDING", 1)
    asyncio.run(calls.start_call(REQUEST))
    with pytest.raises(RuntimeError, match="Too many"):
        asyncio.run(calls.start_call(REQUEST))
