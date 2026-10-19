# -*- coding: utf-8 -*-
"""الوكيل المحلي عبر WebSocket (WS /voice_followup/stream) بنسخ وهمية من
النسخ/النموذج/النطق — يشتغل على CPU بلا موديلات وبلا LiveKit.

  ١. المصادقة والتحقق والسقف: 4401 / 4422 / 4429.
  ٢. المسار الكامل: ready ← accepted ← افتتاحية ← كلام الزبون ← نص ← صوت الـ agent.
  ٣. المقاطعة: كلام الزبون فوق الـ agent ← interrupt وإلغاء الرد.
  ٤. الأدوات: save_outcome ← حدث outcome ونتيجة done بالـ GET؛ end_call ← حدث end_call.
  ٥. النهايات: ended بلا نتيجة ← نتيجة احتياطية، وانتهاء المهلة.
  ٦. الوحدات: تقطيع الأدوار، تقطيع الجمل، تحويل المعدل، سجل الأدوات.
"""

import asyncio
import json
import time
from dataclasses import replace

import anyio
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.features.voice_followup import call_tools, gateway, outcomes, stream, vad
from app.features.voice_followup.schema import AgentCallMetadata

KEY = main.settings.voice_followup_api_key
CALL = {
    "company_name": "متجر النور",
    "agent_name": "سارة",
    "goal": "اسأل الزبون عن رأيه بالغسالة",
    "questions": ["هل وصلك الطلب؟"],
    "contact": {"name": "أمير وسام", "phone": "07701234567"},
    "reference_id": "ORD-1042",
}
SR = 16000
SPEECH = vad.float_to_pcm16(np.full(int(0.3 * SR), 0.5, dtype=np.float32))
SILENCE = vad.float_to_pcm16(np.zeros(int(0.4 * SR), dtype=np.float32))


# ── النسخ الوهمية ───────────────────────────────────────────────────────────

class ScriptedLLM:
    """كل استدعاء ياخذ الدور التالي: قائمة أحداث، أو "hang" = يولّد جملة ثم ينتظر."""

    def __init__(self, *turns):
        self.turns = list(turns)
        self.calls = []

    def __call__(self, messages, tools):
        self.calls.append((json.loads(json.dumps(messages)), [t["function"]["name"] for t in tools]))
        turn = self.turns.pop(0) if self.turns else [{"text": "تمام."}]

        async def gen():
            if turn == "hang":
                yield {"text": "هلا بيك، وياك سارة من متجر النور. "}
                await asyncio.sleep(30)
                return
            for event in turn:
                yield event

        return gen()


def tool_call(name, args, call_id="call_1"):
    return {"tool_calls": [{"id": call_id, "type": "function",
                            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}


class FakeSTT:
    def __init__(self, *texts):
        self.texts = list(texts)

    async def __call__(self, samples, sample_rate):
        assert sample_rate == SR and samples.dtype == np.float32
        return self.texts.pop(0) if self.texts else ""


async def fake_tts(text):
    return np.full(2400, 0.1, dtype=np.float32), 24000  # 0.1s بـ 24kHz


class RecordingSubmitter:
    def __init__(self):
        self.calls = []

    async def submit(self, outcome, api_key):
        self.calls.append(outcome)
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(outcomes, "settings", replace(outcomes.settings, call_outcomes_dir=tmp_path))
    fast = replace(stream.settings, voice_stream_vad="energy", voice_stream_energy_threshold_dbfs=-30,
                   voice_stream_min_speech_ms=100, voice_stream_end_silence_ms=200,
                   voice_stream_idle_timeout_seconds=60, voice_stream_max_calls=5,
                   voice_stream_first_phrase_chars=200)
    monkeypatch.setattr(stream, "settings", fast)
    monkeypatch.setattr(vad, "settings", fast)
    monkeypatch.setattr(stream, "WATCHDOG_TICK_SECONDS", 0.05)
    submitter = RecordingSubmitter()
    monkeypatch.setattr(gateway, "call_outcome_submitter", submitter)

    def install(llm, stt=None, **overrides):
        monkeypatch.setattr(stream, "deps", stream.StreamDeps(
            llm=llm, tts=fake_tts, vad=vad.make_classifier, stt=stt or FakeSTT()))
        if overrides:
            monkeypatch.setattr(stream, "settings", replace(stream.settings, **overrides))
            monkeypatch.setattr(vad, "settings", stream.settings)

    install.submitter = submitter
    return install


def recv(ws, timeout=5.0):
    async def _receive():
        with anyio.fail_after(timeout):
            return await ws._send_rx.receive()
    return ws.portal.call(_receive)


def event(message):
    return json.loads(message["text"]) if message.get("text") is not None else None


def collect_until(ws, predicate, timeout=5.0):
    """يجمع الرسائل لحد حدث JSON يطابق الشرط: (الأحداث، عدد إطارات الصوت)."""
    events, frames = [], 0
    while True:
        message = recv(ws, timeout)
        if message["type"] == "websocket.close":
            events.append({"type": "__close__", "code": message.get("code")})
            if predicate(events[-1]):
                return events, frames
            raise AssertionError(f"انسد الاتصال قبل الحدث المطلوب: {events}")
        if message.get("bytes") is not None:
            frames += 1
            assert len(message["bytes"]) <= SR * stream.CHUNK_MS // 1000 * 2
            continue
        events.append(event(message))
        if predicate(events[-1]):
            return events, frames


def is_type(kind):
    return lambda e: e["type"] == kind


def speak(ws):
    ws.send_bytes(SPEECH)
    ws.send_bytes(SILENCE)


def start(ws, body=None, **extra):
    ws.send_json({"type": "start", "call": body or CALL, "sample_rate": SR, **extra})
    ready = event(recv(ws))
    assert ready["type"] == "ready"
    assert outcomes.ROOM_RE.fullmatch(ready["call_id"]) and ready["call_id"].startswith("call-ORD-1042-")
    return ready["call_id"]


def hang_up(ws):
    """إنهاء نظيف من جهة التطبيق: ended ثم انتظار إغلاق الخادم."""
    ws.send_json({"type": "call_state", "state": "ended"})
    collect_until(ws, is_type("__close__"))


def connect(client, key=KEY, query=False):
    if query:
        return client.websocket_connect(f"/voice_followup/stream?api_key={key}")
    return client.websocket_connect("/voice_followup/stream", headers={"X-API-Key": key})


# ── ١. المصادقة والتحقق والسقف ──────────────────────────────────────────────

def test_rejects_bad_key_with_4401(env):
    env(ScriptedLLM())
    with connect(TestClient(main.app), key="wrong") as ws:
        assert event(recv(ws))["code"] == "unauthorized"
        assert recv(ws)["code"] == 4401


def test_accepts_key_from_query_param(env):
    env(ScriptedLLM())
    with connect(TestClient(main.app), query=True) as ws:
        start(ws)
        ws.send_json({"type": "ping"})
        assert event(recv(ws)) == {"type": "pong"}
        hang_up(ws)


def test_invalid_call_body_is_a_validation_error(env):
    env(ScriptedLLM())
    with connect(TestClient(main.app)) as ws:
        ws.send_json({"type": "start", "call": {"company_name": "X"}, "sample_rate": SR})
        error = event(recv(ws))
        assert error["type"] == "error" and error["code"] == "validation"
        assert recv(ws)["code"] == 4422


def test_first_message_must_be_start(env):
    env(ScriptedLLM())
    with connect(TestClient(main.app)) as ws:
        ws.send_json({"type": "call_state", "state": "accepted"})
        assert event(recv(ws))["code"] == "validation"
        assert recv(ws)["code"] == 4422


def test_busy_when_over_the_cap(env):
    env(ScriptedLLM(), voice_stream_max_calls=1)
    client = TestClient(main.app)
    with connect(client) as first:
        start(first)
        with connect(client) as second:
            second.send_json({"type": "start", "call": CALL, "sample_rate": SR})
            assert event(recv(second))["code"] == "busy"
            assert recv(second)["code"] == 4429
        hang_up(first)


# ── ٢. المسار الكامل ────────────────────────────────────────────────────────

def test_full_turn_ready_accepted_speech_transcript_audio(env):
    llm = ScriptedLLM([{"text": "هلا بيك، وياك سارة من متجر النور. هل وصلك الطلب؟"}],
                      [{"text": "زين، الحمد لله. شنو تقييمك للغسالة؟"}])
    env(llm, stt=FakeSTT("اي وصل"))
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_bytes(SPEECH)  # قبل الرد: يتجاهل
        ws.send_json({"type": "call_state", "state": "ringing"})
        ws.send_json({"type": "call_state", "state": "accepted"})
        events, frames = collect_until(ws, lambda e: e.get("text") == "هل وصلك الطلب؟")
        assert frames > 0
        assert [e["text"] for e in events if e["type"] == "transcript"] == [
            "هلا بيك، وياك سارة من متجر النور.", "هل وصلك الطلب؟"]

        speak(ws)
        events, frames = collect_until(ws, lambda e: e.get("text") == "شنو تقييمك للغسالة؟")
        assert {"type": "transcript", "role": "user", "text": "اي وصل"} in events
        assert frames > 0
        hang_up(ws)

    opening_messages, opening_tools = llm.calls[0]
    assert opening_messages[0]["role"] == "system" and "متجر النور" in opening_messages[0]["content"]
    assert opening_tools == []
    turn_messages, turn_tools = llm.calls[1]
    assert turn_messages[-1]["role"] == "user"
    assert turn_messages[-1]["content"] == "اي وصل"
    assert turn_messages[-2]["role"] == "assistant"
    assert "end_call" in turn_tools
    # الخصوصية: اسم الزبون ورقمه ما يدخلان سياق النموذج.
    dumped = json.dumps(llm.calls, ensure_ascii=False)
    assert "أمير" not in dumped and "07701234567" not in dumped


# ── ٣. المقاطعة ─────────────────────────────────────────────────────────────

def test_barge_in_interrupts_the_agent(env):
    llm = ScriptedLLM("hang", [{"text": "تفضل، أسمعك."}])
    env(llm, stt=FakeSTT("لحظة لحظة"))
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "agent")
        speak(ws)
        events, _ = collect_until(ws, is_type("interrupt"))
        events, _ = collect_until(ws, lambda e: e.get("text") == "تفضل، أسمعك.")
        assert {"type": "transcript", "role": "user", "text": "لحظة لحظة"} in events
        hang_up(ws)
    # الجملة اللي انگالت قبل المقاطعة باقية بالتاريخ.
    messages = llm.calls[1][0]
    assert messages[-2] == {"role": "assistant", "content": "هلا بيك، وياك سارة من متجر النور."}


# ── ٤. الأدوات ──────────────────────────────────────────────────────────────

def test_save_outcome_emits_event_and_outcome_is_done(env):
    llm = ScriptedLLM(
        [{"text": "هلا بيك، هل وصلك الطلب؟"}],
        [tool_call("save_outcome", {"summary": "الطلب وصل والزبون راضي",
                                    "answers": [{"question": "هل وصلك الطلب؟", "answer": "اي وصل"}]})],
    )
    env(llm, stt=FakeSTT("اي وصل وعاجبني"))
    client = TestClient(main.app)
    with connect(client) as ws:
        room = start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "agent")
        speak(ws)
        events, _ = collect_until(ws, is_type("outcome"))
        outcome = events[-1]
        assert outcome["status"] == "done" and outcome["room"] == room
        assert outcome["summary"] == "الطلب وصل والزبون راضي" and outcome["reference_id"] == "ORD-1042"
        assert outcome["customer_transcript"] == "اي وصل وعاجبني"

        events, frames = collect_until(ws, is_type("end_call"))
        assert frames > 0 and events[-1]["reason"] == "agent"
        assert {"type": "transcript", "role": "agent", "text": "شكراً لوقتك، مع السلامة."} in events
        ws.send_json({"type": "call_state", "state": "ended"})
        assert collect_until(ws, is_type("__close__"))[0][-1]["code"] == 1000

    body = client.get(f"/voice_followup/calls/{room}/outcome", headers={"X-API-Key": KEY}).json()
    assert body["status"] == "done" and body["summary"] == "الطلب وصل والزبون راضي"
    assert len(env.submitter.calls) == 1
    # No extra model request is needed to close after a successful save.
    assert len(llm.calls) == 2


def test_wrong_number_end_call_without_outcome_falls_back(env):
    llm = ScriptedLLM([{"text": "هلا بيك، وياك سارة من متجر النور."}],
                      [{"text": "آسفين على الإزعاج، مع السلامة."}, tool_call("end_call", {})])
    env(llm, stt=FakeSTT("رقم غلط"))
    client = TestClient(main.app)
    with connect(client) as ws:
        room = start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "agent")
        speak(ws)
        collect_until(ws, is_type("end_call"))
        ws.send_json({"type": "call_state", "state": "ended"})
        events, _ = collect_until(ws, is_type("outcome"))
        assert events[-1]["summary"] == call_tools.NO_OUTCOME_SUMMARY
        assert events[-1]["follow_up_needed"] is True
        assert events[-1]["customer_transcript"] == "رقم غلط"
    assert outcomes.get_outcome(room).status == "done"


# ── ٥. النهايات ─────────────────────────────────────────────────────────────

def test_ended_before_accept_is_no_answer(env):
    env(ScriptedLLM())
    with connect(TestClient(main.app)) as ws:
        room = start(ws)
        ws.send_json({"type": "call_state", "state": "ringing"})
        ws.send_json({"type": "call_state", "state": "ended"})
        events, _ = collect_until(ws, is_type("outcome"))
        assert events[-1]["summary"] == call_tools.NO_ANSWER_SUMMARY
        assert collect_until(ws, is_type("__close__"))[0][-1]["code"] == 1000
    assert outcomes.get_outcome(room).summary == call_tools.NO_ANSWER_SUMMARY


def test_client_disconnect_still_writes_fallback(env):
    env(ScriptedLLM([{"text": "هلا بيك."}]))
    with connect(TestClient(main.app)) as ws:
        room = start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        collect_until(ws, lambda e: e["type"] == "transcript")
        ws.close()  # انقطاع بلا call_state ended
        for _ in range(100):
            if outcomes.load_outcome(room):
                break
            time.sleep(0.02)
    assert outcomes.get_outcome(room).summary == call_tools.NO_OUTCOME_SUMMARY


def test_idle_timeout_ends_the_call(env):
    env(ScriptedLLM([{"text": "هلا بيك."}]), voice_stream_idle_timeout_seconds=0.3)
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        events, _ = collect_until(ws, is_type("end_call"))
        assert events[-1]["reason"] == "idle_timeout"
        events, _ = collect_until(ws, is_type("outcome"))
        assert events[-1]["summary"] == call_tools.NO_OUTCOME_SUMMARY


def test_llm_failure_reports_error_and_keeps_the_call(env):
    def broken(messages, tools):
        async def gen():
            raise RuntimeError("vLLM down")
            yield  # pragma: no cover
        return gen()

    env(broken)
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        events, _ = collect_until(ws, is_type("error"))
        assert events[-1]["code"] == "llm"
        ws.send_json({"type": "ping"})
        assert collect_until(ws, is_type("pong"))
        hang_up(ws)


def test_tts_failure_reports_reason_and_one_line_log(env, caplog):
    import torch

    async def oom_tts(text):
        raise torch.cuda.OutOfMemoryError("CUDA out of memory. Tried to allocate 20.00 MiB")

    env(ScriptedLLM([{"text": "هلا بيك، وياك سارة من متجر النور."}]))
    stream.deps.tts = oom_tts
    with caplog.at_level("ERROR", logger=stream.__name__):
        with connect(TestClient(main.app)) as ws:
            start(ws)
            ws.send_json({"type": "call_state", "state": "accepted"})
            events, frames = collect_until(ws, is_type("error"))
            assert events[-1] == {"type": "error", "code": "tts", "message": "تعذّر توليد صوت الـ agent",
                                  "reason": "cuda_oom"}
            assert frames == 0
            hang_up(ws)
    lines = [r.getMessage() for r in caplog.records if "فشل نطق جملة" in r.getMessage()]
    assert len(lines) == 1 and "\n" not in lines[0]
    assert "reason=cuda_oom" in lines[0] and "OutOfMemoryError: CUDA out of memory" in lines[0]
    assert "vram_free_gb=" in lines[0]


def test_tts_not_installed_reason(env):
    async def missing(text):
        raise stream.voice_health.NotInstalledError("موديل النطق غير مثبَّت (chatterbox-tts)")

    env(ScriptedLLM([{"text": "هلا بيك، وياك سارة من متجر النور."}]))
    stream.deps.tts = missing
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        events, _ = collect_until(ws, is_type("error"))
        assert events[-1]["reason"] == "not_installed"
        hang_up(ws)


def test_two_consecutive_tts_failures_end_the_call_politely(env):
    async def broken_tts(text):
        raise RuntimeError("generation exploded")

    # الافتتاحية جملتين: الأولى تفشل (error)، الثانية تفشل ← end_call.
    llm = ScriptedLLM([{"text": "هلا بيك، وياك سارة من متجر النور. "}, {"text": "عندي سؤال صغير إذا تسمحلي."}])
    env(llm)
    stream.deps.tts = broken_tts
    with connect(TestClient(main.app)) as ws:
        room = start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        events, frames = collect_until(ws, is_type("end_call"))
        errors = [e for e in events if e["type"] == "error"]
        assert [e["reason"] for e in errors] == ["generation_failed", "generation_failed"]
        assert events[-1] == {"type": "end_call", "reason": "tts_unavailable"}
        assert frames == 0
        events, _ = collect_until(ws, is_type("outcome"))
        assert events[-1]["summary"] == stream.TTS_UNAVAILABLE_SUMMARY
        assert events[-1]["follow_up_needed"] is True
        assert collect_until(ws, is_type("__close__"))[0][-1]["code"] == 1000
    saved = outcomes.get_outcome(room)
    assert saved.status == "done" and saved.follow_up_needed is True


def test_a_successful_sentence_resets_the_tts_failure_count(env):
    calls = []

    async def flaky_tts(text):
        calls.append(text)
        if len(calls) % 2:  # الأولى والثالثة تفشل، الثانية تنجح
            raise RuntimeError("flaky")
        return await fake_tts(text)

    llm = ScriptedLLM([{"text": "هلا بيك، وياك سارة من متجر النور. عندي سؤال صغير إذا تسمحلي. "
                                "هل وصلك الطلب بالسلامة؟"}])
    env(llm)
    stream.deps.tts = flaky_tts
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        events, frames = collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "agent")
        # TTS now runs ahead of playback; both errors can arrive before the
        # successful phrase's transcript. Count them without assuming order.
        events2 = []
        if sum(e["type"] == "error" for e in events) < 2:
            events2, _ = collect_until(ws, is_type("error"))
        assert len(calls) == 3 and frames > 0
        assert not any(e["type"] == "end_call" for e in events + events2)
        hang_up(ws)


# ── ٦. الوحدات ──────────────────────────────────────────────────────────────

def _energy_endpointer():
    return vad.Endpointer(vad.EnergyClassifier(-30), min_speech_ms=100, end_silence_ms=200,
                          max_segment_seconds=2)


def test_endpointer_emits_start_then_segment_with_preroll():
    ep = _energy_endpointer()
    loud = np.full(960, 0.5, dtype=np.float32)
    quiet = np.zeros(960, dtype=np.float32)
    events = []
    for chunk in [quiet] * 3 + [loud] * 5 + [quiet] * 5:
        events += ep.push(chunk)
    assert [e[0] for e in events] == ["start", "end"]
    segment = events[1][1]
    assert segment.size >= 5 * 960 and np.abs(segment).max() == pytest.approx(0.5)
    assert not ep.in_speech


def test_endpointer_ignores_short_noise():
    ep = _energy_endpointer()
    events = ep.push(np.full(800, 0.5, dtype=np.float32)) + ep.push(np.zeros(16000, dtype=np.float32))
    assert events == []


def test_endpointer_cuts_long_monologue():
    ep = _energy_endpointer()
    events = ep.push(np.full(3 * SR, 0.5, dtype=np.float32))
    assert [e[0] for e in events][:2] == ["start", "end"]


def test_silero_classifier_runs_offline_when_available():
    pytest.importorskip("onnxruntime")
    if vad._silero_model_path() is None:
        pytest.skip("أوزان silero غير مثبتة")
    clf = vad.SileroClassifier(0.5)
    assert clf(np.zeros(clf.window, dtype=np.float32)) is False


def test_sentence_splitter_streams_sentences():
    splitter = stream.SentenceSplitter()
    out = []
    for piece in ["هلا بيك، وياك سا", "رة من متجر النور. هل", " وصلك الطلب؟ ", "تمام"]:
        out += splitter.feed(piece)
    out += splitter.flush()
    assert out == ["هلا بيك، وياك سارة من متجر النور.", "هل وصلك الطلب؟", "تمام"]
    long = stream.SentenceSplitter(max_chars=40).feed("كلمة " * 30)
    assert long and all(len(s) <= 40 for s in long)


def test_resample_lengths_and_pcm_roundtrip():
    x = np.sin(np.linspace(0, 100, 24000)).astype(np.float32)
    assert vad.resample(x, 24000, 16000).size == 16000
    assert vad.resample(x[:8000], 8000, 16000).size == 16000
    pcm = vad.float_to_pcm16(np.array([0.0, 0.5, -0.5, 2.0], dtype=np.float32))
    back = vad.pcm16_to_float(pcm)
    assert back[1] == pytest.approx(0.5, abs=1e-3) and back[3] == pytest.approx(1.0, abs=1e-3)


def test_tool_registry_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(outcomes, "settings", replace(outcomes.settings, call_outcomes_dir=tmp_path))
    monkeypatch.setattr(gateway, "call_outcome_submitter", RecordingSubmitter())
    names = [t["function"]["name"] for t in call_tools.openai_tools()]
    assert {"save_outcome", "end_call"} <= set(names)
    ctx = call_tools.CallToolContext(room="call-t-1", meta=AgentCallMetadata(company_name="X", goal="g"))
    run = lambda name, args: asyncio.run(call_tools.run_tool(ctx, name, args))  # noqa: E731
    assert "غير موجودة" in run("drop_tables", {})
    assert "JSON" in run("save_outcome", "{bad")
    assert "فارغ" in run("save_outcome", {"summary": "  "})
    assert "انحفظت" in run("save_outcome", {"summary": "تمام", "answers": [{"question": "q"}, "x"]})
    assert ctx.saved and ctx.outcome.answers == []
    assert "مسبقاً" in run("save_outcome", {"summary": "مرة ثانية"})
    run("end_call", {})
    assert ctx.end_requested
