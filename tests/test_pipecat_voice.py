import asyncio
import io

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.features.voice_followup import calls, pipecat_voice, stream, vad
from app.features.voice_followup.schema import CallRequest
from tests.test_voice_followup_stream import (
    CALL, ScriptedLLM, collect_until, connect, env, event, fake_tts,
    hang_up, is_type, recv, start,
)


def test_voice_summaries_are_visible_without_root_logging_and_not_duplicated(monkeypatch):
    import logging
    import sys
    from app.core.voice_logging import configure_diagnostics

    target = logging.getLogger("app.features.voice_followup.stream")
    monkeypatch.setattr(target, "handlers", [])
    monkeypatch.setattr(target, "propagate", False)
    monkeypatch.setattr(target, "level", logging.WARNING)
    output = io.StringIO()
    monkeypatch.setattr(sys, "stderr", output)
    configure_diagnostics()
    configure_diagnostics()
    target.info("voice_call_end test_probe")
    assert output.getvalue().count("voice_call_end test_probe") == 1


def test_console_example_is_accepted_by_the_real_api(monkeypatch):
    import json
    import re
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "static/index.html").read_text(encoding="utf-8")
    sample = json.loads(re.search(r"const VOICE_CALL_EXAMPLE = (.*);", source).group(1))
    monkeypatch.setattr(calls, "_pending", {})
    CallRequest.model_validate(sample)
    response = TestClient(main.app).post("/voice_followup/calls", json=sample,
                                       headers={"X-API-Key": stream.settings.voice_followup_api_key})
    assert response.status_code == 200 and response.json()["transport"] == "pipecat"


def test_first_phrase_starts_before_a_long_sentence_finishes():
    splitter = stream.SentenceSplitter(first_chars=56)
    assert splitter.feed("مرحبا بيك،") == []
    assert splitter.feed(" وياك سارة من متجر النور،") == ["مرحبا بيك، وياك سارة من متجر النور،"]
    assert splitter.feed(" نريد نسألك عن الطلب") == []
    assert splitter.flush() == ["نريد نسألك عن الطلب"]
    text = "كلمة " * 40 + "."
    phrases = stream.SentenceSplitter(first_chars=56).feed(text)
    assert len(phrases[0]) <= 56
    assert " ".join(phrases).replace(" ", "") == text.replace(" ", "")


def test_splitter_does_not_send_a_partial_word_to_tts():
    splitter = stream.SentenceSplitter(first_chars=16)
    word = "ا" * 25
    assert splitter.feed(word[:18]) == []
    assert splitter.feed(word[18:]) == []
    assert splitter.feed(" ") == [word]
    assert splitter.flush() == []


def test_splitter_keeps_a_streamed_decimal_together():
    splitter = stream.SentenceSplitter()
    assert splitter.feed("السعر هو 12.") == []
    assert splitter.feed("5 دينار.") == ["السعر هو 12.5 دينار."]


def test_pipecat_reads_llm_and_synthesizes_while_playback_is_blocked():
    async def check():
        playback_started, release, generated = asyncio.Event(), asyncio.Event(), asyncio.Event()
        synthesized, spoken = [], []

        async def events():
            yield {"text": "الجملة الأولى. الجملة الثانية."}
            generated.set()

        async def synthesize(text):
            synthesized.append(text)
            return text

        async def emit(text):
            playback_started.set()
            await release.wait()
            spoken.append(text)

        task = asyncio.create_task(pipecat_voice.speak_turn(events(), stream.SentenceSplitter(), synthesize, emit))
        await asyncio.wait_for(playback_started.wait(), 3)
        await asyncio.wait_for(generated.wait(), 1)
        assert len(synthesized) == 2
        release.set()
        await asyncio.wait_for(task, 3)
        assert spoken == synthesized

    asyncio.run(check())


def test_pipecat_cancellation_closes_llm_and_its_tasks():
    async def check():
        started, closed = asyncio.Event(), asyncio.Event()

        async def events():
            try:
                started.set()
                await asyncio.sleep(60)
                yield {"text": "لا يُنطق"}
            finally:
                closed.set()

        async def identity(text):
            return text

        before = set(asyncio.all_tasks())
        task = asyncio.create_task(pipecat_voice.speak_turn(events(), stream.SentenceSplitter(), identity, identity))
        await asyncio.wait_for(started.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        assert closed.is_set()
        await asyncio.sleep(0)
        assert not [t for t in asyncio.all_tasks() - before if not t.done()]

    asyncio.run(check())


def test_pipecat_propagates_tts_failure():
    async def check():
        async def events():
            yield {"text": "جملة مسموعة."}

        async def broken(text):
            raise RuntimeError("test_tts_failure")

        with pytest.raises(RuntimeError, match="test_tts_failure"):
            await pipecat_voice.speak_turn(events(), stream.SentenceSplitter(), broken, broken)

    asyncio.run(check())


def test_opening_is_synthesized_while_ringing_but_only_sent_after_acceptance(env):
    synthesized = []

    async def tts(text):
        synthesized.append(text)
        return await fake_tts(text)

    env(ScriptedLLM([{"text": "مرحبا بيك."}]))
    stream.deps.prewarm_opening = True
    stream.deps.tts = tts
    with connect(TestClient(main.app)) as ws:
        start(ws)

        async def wait_for_tts():
            async with asyncio.timeout(3):
                while not synthesized:
                    await asyncio.sleep(0.01)

        ws.portal.call(wait_for_tts)
        ws.send_json({"type": "ping"})
        assert event(recv(ws)) == {"type": "pong"}
        ws.send_json({"type": "call_state", "state": "accepted"})
        events, frames = collect_until(ws, is_type("transcript"))
        assert frames and events[-1]["role"] == "agent"
        hang_up(ws)


def test_post_call_and_websocket_share_room_and_reject_reuse(env, monkeypatch):
    monkeypatch.setattr(calls, "_pending", {})
    env(ScriptedLLM())
    client = TestClient(main.app)
    response = client.post("/voice_followup/calls", json=CALL, headers={"X-API-Key": stream.settings.voice_followup_api_key})
    data = response.json()
    assert response.status_code == 200 and data["transport"] == "pipecat"
    for attempt in range(2):
        with connect(client) as ws:
            ws.send_json({"type": "start", "call": CALL, "sample_rate": 16000, "stream_id": data["stream_id"]})
            first = event(recv(ws))
            if attempt == 0:
                assert first["call_id"] == data["room"]
                hang_up(ws)
            else:
                assert first["code"] == "validation"


def test_reservations_expire_and_are_not_consumed_on_mismatch(monkeypatch):
    monkeypatch.setattr(calls, "_pending", {})
    request = CallRequest.model_validate(CALL)
    result = asyncio.run(calls.start_call(request))
    with pytest.raises(ValueError):
        calls.consume_stream_id(result.stream_id, request.model_copy(update={"goal": "different"}))
    assert result.stream_id in calls._pending
    monkeypatch.setattr(calls.time, "monotonic", lambda: 10**15)
    with pytest.raises(ValueError):
        calls.consume_stream_id(result.stream_id, request)
    assert not calls._pending


def test_failed_audio_worker_stops_the_call_instead_of_waiting_forever():
    async def check():
        sent = []

        class Socket:
            async def send_text(self, text):
                import json
                sent.append(json.loads(text))

        async def broken():
            raise RuntimeError("worker_test")

        call = stream.StreamCall(Socket(), stream.StreamDeps(
            llm=ScriptedLLM(), tts=fake_tts, vad=lambda: vad.EnergyClassifier(-30)),
            CallRequest.model_validate(CALL), 16000)
        await call._guard_worker(broken)
        assert call._stop.is_set() and call.end_reason == "voice_worker_failed"
        assert sent[-1]["code"] == "voice_worker_failed"

    asyncio.run(check())


def test_audio_backpressure_is_explicit_and_bounded():
    async def check():
        sent = []

        class Socket:
            async def send_text(self, text):
                import json
                sent.append(json.loads(text))

        class Endpointer:
            def push(self, samples):
                return [("end", samples)]

        call = stream.StreamCall(Socket(), stream.StreamDeps(
            llm=ScriptedLLM(), tts=fake_tts, vad=lambda: vad.EnergyClassifier(-30)),
            CallRequest.model_validate(CALL), 16000)
        call.accepted = True
        call.endpointer = Endpointer()
        for _ in range(call._audio_queue.maxsize):
            call._audio_queue.put_nowait(np.ones(320, dtype=np.float32))
        await call._on_audio(b"\0" * 640)
        assert call.end_reason == "audio_backpressure" and call._audio_queue.qsize() == 8
        assert sent[0]["code"] == "audio_backpressure"

    asyncio.run(check())


def test_blocked_websocket_send_times_out_and_cleans_up(monkeypatch):
    async def check():
        class Socket:
            async def send_text(self, text):
                await asyncio.sleep(60)

        call = stream.StreamCall(Socket(), stream.StreamDeps(
            llm=ScriptedLLM(), tts=fake_tts, vad=lambda: vad.EnergyClassifier(-30)),
            CallRequest.model_validate(CALL), 16000)
        await asyncio.wait_for(call._send({"type": "pong"}), 1)
        assert call.disconnected and call._stop.is_set()

    monkeypatch.setattr(stream, "SEND_TIMEOUT_SECONDS", 0.01)
    asyncio.run(check())


@pytest.mark.parametrize("ready,installed,loaded,warm,code", [
    (False, True, True, False, "model_unavailable"),
    (True, False, False, False, "tts_unavailable"),
    (True, True, False, True, "voice_warming"),
])
def test_real_dependencies_are_checked_before_rust_is_told_to_dial(monkeypatch, ready, installed, loaded, warm, code):
    from app.features.voice_followup import tts, voice_health

    monkeypatch.setattr(stream, "deps", None)
    monkeypatch.setattr(main.llm_engine, "_ready", ready)
    monkeypatch.setattr(tts, "status", lambda: {"installed": installed, "loaded": loaded})
    monkeypatch.setattr(voice_health, "warmup_enabled", lambda: warm)
    with connect(TestClient(main.app)) as ws:
        ws.send_json({"type": "start", "call": CALL, "sample_rate": 16000})
        first = event(recv(ws))
        assert first["type"] == "error" and first["code"] == code
        assert recv(ws)["code"] == 1013


@pytest.mark.parametrize("installed,code", [(False, "stt_unavailable"), (True, "voice_warming")])
def test_whisper_is_checked_before_the_customer_is_dialed(monkeypatch, installed, code):
    from app.features.voice_followup import stt, tts, voice_health
    monkeypatch.setattr(stream, "deps", None)
    monkeypatch.setattr(main.llm_engine, "_ready", True)
    monkeypatch.setattr(tts, "status", lambda: {"installed": True, "loaded": True})
    monkeypatch.setattr(stt, "status", lambda: {"installed": installed, "loaded": False})
    monkeypatch.setattr(voice_health, "warmup_enabled", lambda: True)
    with connect(TestClient(main.app)) as ws:
        ws.send_json({"type": "start", "call": CALL, "sample_rate": 16000})
        assert event(recv(ws))["code"] == code
        assert recv(ws)["code"] == 1013
