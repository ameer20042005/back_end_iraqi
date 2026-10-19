import asyncio
import json
import threading
import time
from dataclasses import replace

from fastapi.testclient import TestClient

from app import main
from app.features.voice_followup import call_tools, gateway, outcomes, stream
from tests.test_voice_followup_stream import (
    FakeSTT, ScriptedLLM, collect_until, connect, env, fake_tts, hang_up,
    is_type, speak, start, tool_call,
)


def test_save_closes_even_when_goodbye_synthesis_fails(env):
    env(ScriptedLLM([{"text": "هل وصلك الطلب؟"}],
                    [tool_call("save_outcome", {"summary": "وصل الطلب"})]), stt=FakeSTT("اي وصل"))

    async def tts(text):
        if text == "شكراً لوقتك، مع السلامة.":
            raise RuntimeError("goodbye_synthesis_failed")
        return await fake_tts(text)

    stream.deps.tts = tts
    with connect(TestClient(main.app)) as ws:
        room = start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        collect_until(ws, lambda e: e.get("role") == "agent")
        speak(ws)
        events, _ = collect_until(ws, is_type("end_call"))
        assert any(e["type"] == "outcome" for e in events)
        assert events[-1]["reason"] == "agent"
        assert outcomes.load_outcome(room).summary == "وصل الطلب"
        hang_up(ws)


def test_invalid_save_keeps_the_conversation_open(env):
    env(ScriptedLLM([{"text": "هل وصلك الطلب؟"}],
                    [tool_call("save_outcome", {"summary": " "})],
                    [{"text": "ممكن توضحلي إذا استلمت الطلب؟"}]), stt=FakeSTT("ما أدري"))
    with connect(TestClient(main.app)) as ws:
        room = start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        collect_until(ws, lambda e: e.get("role") == "agent")
        speak(ws)
        events, _ = collect_until(ws, lambda e: e.get("role") == "agent")
        assert not any(e["type"] in ("outcome", "end_call") for e in events)
        assert outcomes.load_outcome(room) is None
        hang_up(ws)


def test_barge_in_during_save_cannot_leave_a_completed_call_open(env, monkeypatch):
    began, release = threading.Event(), threading.Event()

    class Submitter:
        async def submit(self, outcome, api_key):
            began.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            return True

    llm = ScriptedLLM([{"text": "هل وصلك الطلب؟"}],
                      [tool_call("save_outcome", {"summary": "وصل الطلب"})])
    env(llm, stt=FakeSTT("اي وصل"))
    monkeypatch.setattr(gateway, "call_outcome_submitter", Submitter())
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        collect_until(ws, lambda e: e.get("role") == "agent")
        speak(ws)
        assert began.wait(3)
        speak(ws)
        ws.send_json({"type": "ping"})
        collect_until(ws, is_type("pong"))
        release.set()
        collect_until(ws, is_type("end_call"))
        hang_up(ws)
    assert len(llm.calls) == 2


def test_outcome_forwarding_timeout_keeps_local_result(env, monkeypatch):
    cancelled = []

    class SlowSubmitter:
        async def submit(self, *args):
            try:
                await asyncio.sleep(60)
            finally:
                cancelled.append(True)

    env(ScriptedLLM())
    monkeypatch.setattr(call_tools, "settings", replace(call_tools.settings, call_outcome_submit_timeout_seconds=0.02))
    from app.features.voice_followup.schema import AgentCallMetadata
    context = call_tools.CallToolContext(room="call-slow-forward", meta=AgentCallMetadata(company_name="X", goal="g"))
    monkeypatch.setattr(gateway, "call_outcome_submitter", SlowSubmitter())
    started = time.monotonic()
    asyncio.run(call_tools.run_tool(context, "save_outcome", {"summary": "وصل الطلب"}))
    assert time.monotonic() - started < 1
    assert cancelled == [True] and context.saved
    assert outcomes.load_outcome(context.room).summary == "وصل الطلب"


def test_hangup_waits_for_last_audio_to_play(env, monkeypatch):
    from app.features.voice_followup.schema import CallRequest
    from tests.test_voice_followup_stream import CALL
    sent = []

    class Socket:
        async def send_text(self, raw):
            sent.append((time.monotonic(), json.loads(raw)))

    async def check():
        call = stream.StreamCall(Socket(), stream.get_deps(), CallRequest.model_validate(CALL), 16000)
        due = time.monotonic() + 0.03
        call._playback_until = due
        await call._agent_end()
        assert sent[-1][0] >= due and sent[-1][1]["type"] == "end_call"
        assert call._stop.is_set()

    env(ScriptedLLM())
    monkeypatch.setattr(stream, "END_GRACE_SECONDS", 0)
    asyncio.run(check())
