# -*- coding: utf-8 -*-
"""صوت الزبون الداخل بالوكيل المحلي (WS /voice_followup/stream): الإحصاءات
والتشخيص، AGC، القطع ذات البايتات الفردية، والنسخ وقت كلام الـ agent.

  ١. إحصاءات كل مكالمة: باللوق كل STATS_LOG_SECONDS وبالنهاية، وبـ /health/voice (last_call) بلا نص.
  ٢. التشخيص: no_audio / too_quiet / whisper_empty / stt_error / ok.
  ٣. AGC: الصوت الواطي يوصل لـ VAD وWhisper؛ بلا خفض وبسقف رفع.
  ٤. الصوت وقت كلام الـ agent يتنسخ (مو بس يقاطع)، وقطع بعدد بايتات فردي ما تخرّب العينات.
"""

import json
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import main
from app.features.voice_followup import call_stats, stream, vad, voice_health

from tests.test_voice_followup_stream import (  # noqa: F401 — env fixture
    SILENCE, SPEECH, SR, FakeSTT, ScriptedLLM, collect_until, connect, env, hang_up, is_type, start,
)


def tone(dbfs: float, seconds: float = 0.3) -> bytes:
    return vad.float_to_pcm16(np.full(int(seconds * SR), 10 ** (dbfs / 20), dtype=np.float32))


def accept_and_wait_for_agent(ws):
    start(ws)
    ws.send_json({"type": "call_state", "state": "accepted"})
    collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "agent")


# ── ١. الإحصاءات ───────────────────────────────────────────────────────────

def test_stats_are_logged_periodically_and_at_the_end_without_text(env, monkeypatch, caplog):
    monkeypatch.setattr(stream, "STATS_LOG_SECONDS", 0.05)
    env(ScriptedLLM([{"text": "هلا بيك."}], [{"text": "تمام."}]), stt=FakeSTT("اسمي أمير ورقمي 0770"))
    with caplog.at_level("INFO", logger=stream.__name__):
        with connect(TestClient(main.app)) as ws:
            accept_and_wait_for_agent(ws)
            ws.send_bytes(SPEECH)
            ws.send_bytes(SILENCE)
            collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "user")
            collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "agent")
            time.sleep(0.2)  # يكفي لعدة دورات لوق دورية
            hang_up(ws)
    lines =[r.getMessage() for r in caplog.records if "إحصاءات صوت المكالمة" in r.getMessage()]
    assert len(lines) >= 2 and "(النهاية)" in lines[-1]
    assert all("\n" not in line and "أمير" not in line and "0770" not in line for line in lines)
    assert "diagnosis=ok" in lines[-1] and "vad=energy" in lines[-1]

    last = voice_health.last_call()
    assert last["active"] is False and last["diagnosis"] == "ok"
    assert last["audio_bytes"] == len(SPEECH) + len(SILENCE)
    assert last["segments_to_whisper"] == 1 and last["user_transcripts"] == 1 and last["whisper_empty"] == 0
    assert last["vad_speech_windows"] > 0 and last["turn_starts"] == 1
    assert last["level_dbfs_max"] == pytest.approx(-6.0, abs=0.2)  # SPEECH = 0.5
    assert "أمير" not in json.dumps(last, ensure_ascii=False)

    body = TestClient(main.app).get("/health/voice").json()
    assert body["last_call"] == last


def test_whisper_empty_is_diagnosed(env):
    env(ScriptedLLM([{"text": "هلا بيك."}]), stt=FakeSTT(""))
    with connect(TestClient(main.app)) as ws:
        accept_and_wait_for_agent(ws)
        ws.send_bytes(SPEECH)
        ws.send_bytes(SILENCE)
        ws.send_json({"type": "ping"})
        collect_until(ws, is_type("pong"))
        hang_up(ws)
    last = voice_health.last_call()
    assert last["segments_to_whisper"] == 1 and last["whisper_empty"] == 1
    assert last["diagnosis"] == "whisper_empty"


def test_stt_error_is_diagnosed(env):
    async def broken(samples, rate):
        raise ValueError("generation config is outdated")

    env(ScriptedLLM([{"text": "هلا بيك."}]), stt=broken)
    with connect(TestClient(main.app)) as ws:
        accept_and_wait_for_agent(ws)
        ws.send_bytes(SPEECH)
        ws.send_bytes(SILENCE)
        assert collect_until(ws, is_type("error"))[0][-1]["code"] == "stt"
        hang_up(ws)
    last = voice_health.last_call()
    assert last["diagnosis"] == "stt_error" and last["stt_error_type"] == "ValueError"


def test_audio_before_accept_is_counted_as_ignored(env):
    env(ScriptedLLM())
    with connect(TestClient(main.app)) as ws:
        start(ws)
        ws.send_bytes(SPEECH)
        hang_up(ws)
    last = voice_health.last_call()
    assert last["ignored_bytes"] == len(SPEECH) and last["audio_bytes"] == 0
    assert last["diagnosis"] == "no_audio"


# ── ٢. التشخيص (وحدة) ─────────────────────────────────────────────────────

def _stats(**kw):
    s = call_stats.CallAudioStats(16000, "silero", True)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_diagnosis_rules():
    assert _stats().diagnosis() == "no_audio"
    assert _stats(audio_bytes=10, vad_windows=100, vad_speech_windows=0, level_max=-62.0).diagnosis() == "too_quiet"
    assert _stats(audio_bytes=10, vad_windows=100, vad_speech_windows=0, level_max=-25.0).diagnosis() == "vad_no_speech"
    assert _stats(audio_bytes=10, segments=2, whisper_empty=2).diagnosis() == "whisper_empty"
    assert _stats(audio_bytes=10, segments=2, stt_errors=2).diagnosis() == "stt_error"
    assert _stats(audio_bytes=10, vad_windows=5, vad_speech_windows=0, user_transcripts=1).diagnosis() == "ok"
    assert _stats(audio_bytes=10, vad_windows=5, vad_speech_windows=3).diagnosis() == "no_speech_yet"


def test_sample_rate_mismatch_from_arrival_speed(monkeypatch):
    clock = iter([0.0, 0.0, 4.0])
    monkeypatch.setattr(call_stats.time, "monotonic", lambda: next(clock))
    s = call_stats.CallAudioStats(16000, "silero", True)
    # 4 ثواني بالوقت الحقيقي وصلت 48000×4 عينة = التطبيق يرسل 48kHz ويعلن 16kHz.
    s.on_audio(48000 * 4 * 2, np.zeros(10, dtype=np.float32))
    s.on_audio(0, np.zeros(10, dtype=np.float32))
    assert s.measured_sample_rate() == 48000 and s.diagnosis() == "sample_rate_mismatch"


def test_level_stats_are_raw_dbfs():
    s = call_stats.CallAudioStats(16000, "energy", False)
    s.on_audio(640, np.concatenate([np.full(320, 0.1, np.float32), np.zeros(320, np.float32)]))
    snap = s.snapshot()
    assert snap["level_dbfs_max"] == pytest.approx(-20.0, abs=0.1)
    assert snap["level_dbfs_mean"] == pytest.approx((-20.0 + call_stats.SILENCE_DBFS) / 2, abs=0.1)


# ── ٣. AGC ─────────────────────────────────────────────────────────────────

def test_agc_boosts_quiet_audio_up_to_the_cap_and_never_attenuates():
    agc = vad.Agc(target_dbfs=-20, max_gain_db=20)
    quiet = np.full(960, 10 ** (-35 / 20), dtype=np.float32)
    out = agc.process(quiet)
    assert 20 * np.log10(np.abs(out).max()) == pytest.approx(-20, abs=0.1)

    very_quiet = vad.Agc(target_dbfs=-20, max_gain_db=20).process(np.full(960, 10 ** (-60 / 20), dtype=np.float32))
    assert 20 * np.log10(np.abs(very_quiet).max()) == pytest.approx(-40, abs=0.1)  # سقف +20dB

    loud = np.full(960, 0.5, dtype=np.float32)
    assert np.array_equal(vad.Agc().process(loud), loud)
    assert not vad.Agc().process(np.zeros(960, dtype=np.float32)).any()


def test_agc_does_not_pump_right_after_speech():
    agc = vad.Agc(target_dbfs=-20, max_gain_db=20, release_seconds=3.0)
    agc.process(np.full(960, 0.1, dtype=np.float32))  # كلام بمستوى الهدف
    agc.process(np.full(960, 0.001, dtype=np.float32))  # سكوت قصير بعده
    assert agc.gain < 1.1  # المستوى ينزل ببطء، فالربح ما يقفز


def test_quiet_call_is_too_quiet_without_agc_and_heard_with_it(env):
    quiet = tone(-55)
    for agc_on, expect_transcript in ((False, False), (True, True)):
        env(ScriptedLLM([{"text": "هلا بيك."}], [{"text": "تمام."}]), stt=FakeSTT("اي"),
            voice_stream_agc=agc_on, voice_stream_energy_threshold_dbfs=-40)
        with connect(TestClient(main.app)) as ws:
            accept_and_wait_for_agent(ws)
            ws.send_bytes(quiet)
            ws.send_bytes(SILENCE)
            if expect_transcript:
                collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "user")
            else:
                ws.send_json({"type": "ping"})
                events, _ = collect_until(ws, is_type("pong"))
                assert not any(e["type"] == "transcript" and e["role"] == "user" for e in events)
            hang_up(ws)
        last = voice_health.last_call()
        assert last["agc"] is agc_on and last["level_dbfs_max"] == pytest.approx(-55, abs=0.3)
        if agc_on:
            assert last["diagnosis"] == "ok" and last["agc_gain_db_max"] == pytest.approx(20, abs=0.1)
        else:
            assert last["diagnosis"] == "too_quiet" and last["vad_speech_windows"] == 0


# ── ٤. الصوت وقت كلام الـ agent، والبايتات الفردية ──────────────────────────

def test_customer_speech_during_agent_playback_is_transcribed(env):
    async def long_tts(text):
        return np.full(24000 * 3, 0.1, dtype=np.float32), 24000  # 3 ثواني تشغيل

    env(ScriptedLLM([{"text": "هلا بيك، وياك سارة من متجر النور."}], [{"text": "تفضل."}]),
        stt=FakeSTT("لحظة"))
    stream.deps.tts = long_tts
    with connect(TestClient(main.app)) as ws:
        accept_and_wait_for_agent(ws)  # الصوت انرسل كله بس التطبيق بعده يشغّله
        ws.send_bytes(SPEECH)
        ws.send_bytes(SILENCE)
        events, _ = collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "user")
        assert {"type": "interrupt"} in events and events[-1]["text"] == "لحظة"
        hang_up(ws)
    assert voice_health.last_call()["interrupts"] == 1


def test_odd_sized_audio_frames_keep_sample_alignment(env):
    seen = []

    async def capture(samples, rate):
        seen.append(samples)
        return "اي"

    env(ScriptedLLM([{"text": "هلا بيك."}], [{"text": "تمام."}]), stt=capture)
    audio = SPEECH + SILENCE
    with connect(TestClient(main.app)) as ws:
        accept_and_wait_for_agent(ws)
        for i in range(0, len(audio), 961):  # قطع بعدد بايتات فردي
            ws.send_bytes(audio[i:i + 961])
        collect_until(ws, lambda e: e["type"] == "transcript" and e["role"] == "user")
        hang_up(ws)
    # بلا إزاحة بايت: الكلام يبقى 0.5 بالضبط (الإزاحة كانت تطلّع قيم عشوائية).
    speech = seen[0][np.abs(seen[0]) > 0.01]
    assert speech.size and np.allclose(speech, 0.5, atol=1e-3)


def test_endpointer_counts_windows():
    ep = vad.Endpointer(vad.EnergyClassifier(-30), min_speech_ms=100, end_silence_ms=200, max_segment_seconds=2)
    ep.push(vad.pcm16_to_float(SPEECH + SILENCE))
    assert ep.windows == (len(SPEECH) + len(SILENCE)) // 2 // 320
    assert ep.speech_windows == len(SPEECH) // 2 // 320 and ep.starts == 1
