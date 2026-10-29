import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.features.voice_followup import stream, vad
from tests.test_voice_followup_stream import (
    FakeSTT, ScriptedLLM, collect_until, connect, env, hang_up, is_type,
    speak, start,
)


def test_tts_padding_is_trimmed_without_removing_internal_pauses():
    sr = 24000
    speech = np.full(sr // 5, 0.05, dtype=np.float32)
    middle_pause = np.zeros(sr // 4, dtype=np.float32)
    body = np.concatenate([speech, middle_pause, speech])
    audio = np.concatenate([np.zeros(sr), body, np.zeros(sr)])
    trimmed = vad.trim_tts_padding(audio, sr)
    assert trimmed.size == body.size + int(sr * 0.04) + int(sr * 0.25)
    assert np.array_equal(trimmed[int(sr * 0.04):-int(sr * 0.25)], body)


def test_tts_keeps_quiet_consonants_at_the_edges():
    audio = np.concatenate([np.full(1000, 0.0005), np.full(1000, 0.02), np.full(1000, 0.0005)])
    assert np.array_equal(vad.trim_tts_padding(audio, 16000), audio)
    assert vad.trim_tts_padding(np.zeros(16000), 16000).size == 0


@pytest.mark.parametrize("audio", [np.array([np.nan]), np.ones((2, 100))])
def test_invalid_tts_is_rejected_before_pcm_conversion(audio):
    with pytest.raises(ValueError):
        vad.trim_tts_padding(audio, 16000)


def test_repeated_model_phrase_is_not_synthesized_twice_in_one_turn(env):
    phrase = "وياك سارة من متجر النور."
    llm = ScriptedLLM([{"text": f"{phrase} {phrase} شنو رأيك بالخدمة؟"}])
    env(llm)
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        events, _ = collect_until(ws, lambda e: e.get("text") == "شنو رأيك بالخدمة؟")
        assert sum(e.get("text") == phrase for e in events) == 1
        hang_up(ws)


def test_customer_can_request_the_same_phrase_again_on_a_later_turn(env):
    phrase = "وياك سارة من متجر النور."
    env(ScriptedLLM([{"text": phrase}], [{"text": phrase}]), stt=FakeSTT("منو وياي؟"))
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        collect_until(ws, lambda e: e.get("text") == phrase)
        speak(ws)
        events, frames = collect_until(ws, lambda e: e.get("text") == phrase)
        assert frames and any(e.get("role") == "user" for e in events)
        hang_up(ws)


def test_interrupted_introduction_is_retained_without_claiming_it_finished(env):
    intro = "هلا بيك، وياك سارة من متجر النور."
    llm = ScriptedLLM([{"text": intro}], [{"text": "تفضل أسمعك."}])
    env(llm, stt=FakeSTT("ألو تسمعني؟"), voice_stream_tts_lead_ms=0)

    async def long_tts(text):
        return np.full(24000 * 3, 0.1, dtype=np.float32), 24000

    stream.deps.tts = long_tts
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_json({"type": "call_state", "state": "accepted"})
        # Interrupt after the first PCM frame, before a complete transcript.
        from tests.test_voice_followup_stream import recv
        assert recv(ws).get("bytes")
        speak(ws)
        collect_until(ws, is_type("interrupt"))
        collect_until(ws, lambda e: e.get("text") == "تفضل أسمعك.")
        hang_up(ws)

    messages = llm.calls[1][0]
    previous = next(m["content"] for m in messages if m["role"] == "assistant")
    assert intro in previous and "قوطع الكلام قبل اكتماله" in previous
    assert messages[-1] == {"role": "user", "content": "ألو تسمعني؟"}
