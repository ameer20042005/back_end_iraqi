# -*- coding: utf-8 -*-
"""الوكيل المحلي: مكالمة صوتية كاملة عبر WebSocket بلا LiveKit — WS /voice_followup/stream.

تطبيق سطح المكتب يتصل بالزبون من رقم الواتساب مالته، ويبث صوت الزبون لهنا
(PCM s16le أحادي)، وهذا الخادم يدير الحوار بنفس شخصية وتعليمات وأدوات الـ agent
بـ LiveKit (prompts.py + call_tools.py):

    صوت الزبون ─► VAD/تقطيع الأدوار (vad.py) ─► Whisper (stt.py)
        ─► النموذج (vLLM، رد متدفق + أدوات) ─► Chatterbox جملة جملة (tts.py)
        ─► PCM 16kHz للتطبيق

العقد الكامل بـ docs/api/voice-followup.md (قسم "الوكيل المحلي عبر WebSocket").

**المقاطعة (barge-in):** أول ما يبدأ الزبون يحچي (كلام متواصل ≥ min_speech_ms)
والـ agent يولّد أو صوته بعده يشتغل عند التطبيق، تنلغي مهمة الرد (النموذج +
النطق) وينرسل {"type":"interrupt"} حتى يفرّغ التطبيق صوته المخزّن. الإرسال
يسبق التشغيل بـ voice_stream_tts_lead_ms بس، فاللي ينرمى عند المقاطعة قليل.

**قابلية الاختبار:** النسخ والنموذج والنطق وكاشف الكلام كلها بـ StreamDeps،
والاختبارات تبدّل `stream.deps` بنسخ وهمية تشتغل على CPU بلا موديلات.
"""

import asyncio
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Dict, List, Optional, Tuple

import numpy as np
from fastapi import WebSocket
from pydantic import ValidationError

from app.config import settings
from app.features.voice_followup import call_tools, vad, voice_health
from app.features.voice_followup.calls import agent_metadata, room_name
from app.features.voice_followup.prompts import build_call_instructions, build_opening_instruction
from app.features.voice_followup.schema import CallRequest

logger = logging.getLogger(__name__)

# رموز إغلاق WebSocket الخاصة بهذا العقد (نطاق 4000-4999 للتطبيقات).
CLOSE_UNAUTHORIZED = 4401
CLOSE_TIMEOUT = 4408
CLOSE_VALIDATION = 4422
CLOSE_BUSY = 4429

START_TIMEOUT_SECONDS = 15.0
# بعد end_call: مهلة للتطبيق حتى يكمّل تشغيل الجملة الأخيرة ويقفل الخط.
END_GRACE_SECONDS = 10.0
# صوت الـ agent ينبعث بقطع 40ms حتى يقدر التطبيق يوقفه بسرعة عند المقاطعة.
CHUNK_MS = 40
MAX_TOOL_ROUNDS = 4
WATCHDOG_TICK_SECONDS = 0.5

OPENING_PREFIX = "(تعليمات داخلية من النظام، مو كلام الزبون) "

# فشل النطق هذا العدد من المرات ورا بعض بنفس المكالمة = الزبون بصمت، فننهيها.
MAX_CONSECUTIVE_TTS_FAILURES = 2
TTS_UNAVAILABLE_SUMMARY = "تعذّر توليد صوت الـ agent فانتهت المكالمة — تحتاج متابعة"

# ---------------------------------------------------------------------------
# التبعيات الثقيلة (قابلة للتبديل بالاختبارات)
# ---------------------------------------------------------------------------

SttFn = Callable[[np.ndarray, int], Awaitable[Optional[str]]]
# يرجع أحداث {"text": "..."} أثناء التوليد، و{"tool_calls": [...]} مرة بالنهاية.
LlmFn = Callable[[List[dict], List[dict]], AsyncIterator[Dict[str, Any]]]
TtsFn = Callable[[str], Awaitable[Optional[Tuple[np.ndarray, int]]]]


@dataclass
class StreamDeps:
    stt: SttFn
    llm: LlmFn
    tts: TtsFn
    vad: Callable[[], Any]


async def whisper_stt(samples: np.ndarray, sample_rate: int) -> Optional[str]:
    from app.features.voice_followup import stt as whisper

    text = await asyncio.to_thread(whisper.transcribe_pcm, samples, sample_rate)
    if text is None:
        raise voice_health.NotInstalledError("موديل النسخ غير مثبَّت (transformers)")
    return text


async def chatterbox_tts(text: str) -> Optional[Tuple[np.ndarray, int]]:
    from app.features.voice_followup import tts as chatterbox

    result = await asyncio.to_thread(chatterbox.synthesize_array, text)
    if result is None:
        raise voice_health.NotInstalledError("موديل النطق غير مثبَّت (chatterbox-tts)")
    return result


async def vllm_stream(messages: List[dict], tools: List[dict]) -> AsyncIterator[Dict[str, Any]]:
    """نفس خادم vLLM ونفس إعدادات الـ agent بـ LiveKit (agent._llm): بلا تفكير،
    temperature=0، ورد متدفق حتى تبدأ أول جملة بالنطق قبل ما يكمل الرد."""
    from app.core.engine import (
        AI_UPSTREAM_ERROR,
        LM_STUDIO_UNAVAILABLE,
        LLMUpstreamError,
        _upstream_error_message,
        llm_engine,
    )

    client = llm_engine._client
    if client is None or not llm_engine.ready:
        raise LLMUpstreamError("خادم النموذج غير متاح حالياً", code=LM_STUDIO_UNAVAILABLE)
    body = {
        "model": settings.model_name,
        "messages": messages,
        "max_tokens": settings.voice_stream_llm_max_tokens,
        "temperature": 0.0,
        "stream": True,
        "reasoning_effort": "none",
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    calls: Dict[int, Dict[str, str]] = {}
    async with client.stream("POST", "/chat/completions", json=body) as resp:
        if resp.status_code >= 400:
            await resp.aread()
            raise LLMUpstreamError(_upstream_error_message(resp), upstream_status=resp.status_code,
                                   code=AI_UPSTREAM_ERROR)
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if isinstance(delta.get("content"), str) and delta["content"]:
                    yield {"text": delta["content"]}
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    slot["id"] = tc.get("id") or slot["id"]
                    slot["name"] = slot["name"] or fn.get("name") or ""
                    if isinstance(fn.get("arguments"), str):
                        slot["arguments"] += fn["arguments"]
    if calls:
        yield {"tool_calls": [
            {"id": s["id"] or f"call_{uuid.uuid4().hex}", "type": "function",
             "function": {"name": s["name"], "arguments": s["arguments"] or "{}"}}
            for _, s in sorted(calls.items()) if s["name"]
        ]}


def default_deps() -> StreamDeps:
    return StreamDeps(stt=whisper_stt, llm=vllm_stream, tts=chatterbox_tts, vad=vad.make_classifier)


# None = التبعيات الحقيقية؛ الاختبارات تضبطها بنسخ وهمية.
deps: Optional[StreamDeps] = None


def get_deps() -> StreamDeps:
    return deps or default_deps()


# ---------------------------------------------------------------------------
# تقطيع رد النموذج لجمل
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"[.!?؟؛…\n]+")
_SOFT_BREAK = re.compile(r"[،,:]\s|\s")


class SentenceSplitter:
    """يجمع نص النموذج المتدفق ويطلّع جمل كاملة تنطق وحدة وحدة. الجملة الأقصر
    من min_chars تنضم للي بعدها (Chatterbox ينطق الجمل الطويلة شوي أطبع)."""

    def __init__(self, min_chars: int = 8, max_chars: int = 200) -> None:
        self._buf = ""
        self._min = min_chars
        self._max = max_chars

    def feed(self, text: str) -> List[str]:
        self._buf += text
        out = []
        while True:
            cut = None
            for m in _SENTENCE_END.finditer(self._buf):
                if len(self._buf[:m.end()].strip()) >= self._min:
                    cut = m.end()
                    break
            if cut is None and len(self._buf) > self._max:
                breaks = [m.end() for m in _SOFT_BREAK.finditer(self._buf, 0, self._max)]
                cut = breaks[-1] if breaks else self._max
            if cut is None:
                return out
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if sentence:
                out.append(sentence)

    def flush(self) -> List[str]:
        rest, self._buf = self._buf.strip(), ""
        return [rest] if rest else []


def clean_sentence(text: str) -> str:
    # نفس تنظيف قنوات القالب المسرّبة بمساعد الموظف.
    from app.features.voice_followup.operator import clean_content

    return clean_content(text)


# ---------------------------------------------------------------------------
# جلسة مكالمة واحدة
# ---------------------------------------------------------------------------

_active_calls = 0


class TtsUnavailable(Exception):
    """النطق فشل MAX_CONSECUTIVE_TTS_FAILURES مرات ورا بعض: المكالمة تنتهي."""


class StreamCall:
    def __init__(self, ws: WebSocket, deps_: StreamDeps, request: CallRequest, sample_rate: int) -> None:
        self.ws = ws
        self.deps = deps_
        self.sample_rate = sample_rate
        self.meta = agent_metadata(request)  # بلا اسم الزبون
        self.room = room_name(request)
        self.instructions = build_call_instructions(self.meta, call_tools.data_tools_enabled())
        self.history: List[dict] = []
        self.customer_lines: List[str] = []
        self.tools = call_tools.CallToolContext(
            room=self.room, meta=self.meta, customer_transcript=self.customer_transcript,
        )
        self.endpointer = vad.Endpointer(deps_.vad())

        self.accepted = False
        self.ending = False          # end_call انطلب: ما نقبل أدوار جديدة ولا مقاطعة
        self.disconnected = False
        self.end_reason = ""
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._stt_queue: "asyncio.Queue[np.ndarray]" = asyncio.Queue()
        self._response: Optional[asyncio.Task] = None
        self._turn_audio_sent = False
        self._user_turn_pending = False
        self._playback_until = 0.0
        self._started_at = time.monotonic()
        self._last_activity = self._started_at
        self._sent_outcome = False
        self._tts_failures = 0
        self._fallback_summary: Optional[str] = None

    # ── الإرسال ─────────────────────────────────────────────────────────────

    async def _send(self, payload) -> None:
        if self.disconnected:
            return

        async def do():
            async with self._send_lock:
                if self.disconnected:
                    return
                try:
                    if isinstance(payload, bytes):
                        await self.ws.send_bytes(payload)
                    else:
                        await self.ws.send_text(json.dumps(payload, ensure_ascii=False))
                except Exception:
                    self.disconnected = True
                    self.stop("disconnected")

        # shield: إلغاء مهمة الرد (مقاطعة) ما يقطع إطاراً بنصه.
        await asyncio.shield(do())

    def stop(self, reason: str) -> None:
        if not self._stop.is_set():
            self.end_reason = reason
            self._stop.set()

    def customer_transcript(self) -> str:
        return "\n".join(self.customer_lines)

    # ── الحلقة الرئيسية ────────────────────────────────────────────────────

    async def run(self) -> None:
        await self._send({"type": "ready", "call_id": self.room})
        tasks = [asyncio.create_task(self._receive_loop()), asyncio.create_task(self._stt_worker()),
                 asyncio.create_task(self._watchdog())]
        try:
            await self._stop.wait()
        finally:
            pending = tasks + ([self._response] if self._response else [])
            for task in pending:
                task.cancel()
            # النتيجة الاحتياطية تنكتب قبل أي await حتى لو انلغى الاتصال نفسه
            # (إيقاف الخادم مثلاً).
            fallback = self._write_fallback()
            await asyncio.gather(*pending, return_exceptions=True)
            await self._finalize(fallback)

    def _write_fallback(self):
        if self.tools.saved:
            return None
        summary = self._fallback_summary or (
            call_tools.NO_OUTCOME_SUMMARY if self.accepted else call_tools.NO_ANSWER_SUMMARY)
        return call_tools.save_fallback_outcome(self.meta, self.room, summary, self.customer_transcript())

    async def _finalize(self, fallback) -> None:
        if fallback is not None:
            await self._send({"type": "outcome", **fallback.model_dump()})
        logger.info("انتهت مكالمة WebSocket %s (%s)", self.room, self.end_reason or "cancelled")
        if not self.disconnected:
            try:
                await self.ws.close(code=1000)
            except Exception:
                pass

    async def _receive_loop(self) -> None:
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                self.disconnected = True
                self.stop("disconnected")
                return
            if message.get("bytes") is not None:
                await self._on_audio(message["bytes"])
            elif message.get("text") is not None:
                await self._on_text(message["text"])

    async def _on_text(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            msg = None
        if not isinstance(msg, dict):
            await self._send({"type": "error", "code": "protocol", "message": "رسالة نصية مو كائن JSON"})
            return
        kind = msg.get("type")
        if kind == "ping":
            await self._send({"type": "pong"})
        elif kind == "call_state":
            state = msg.get("state")
            if state == "accepted" and not self.accepted:
                self.accepted = True
                self._last_activity = time.monotonic()
                await self._start_response(opening=True)
            elif state == "ended":
                self.stop("customer_hangup")
            elif state not in ("ringing", "accepted"):
                await self._send({"type": "error", "code": "protocol", "message": f"حالة غير معروفة: {state!r}"})
        else:
            await self._send({"type": "error", "code": "protocol", "message": f"نوع رسالة غير معروف: {kind!r}"})

    async def _on_audio(self, data: bytes) -> None:
        if not self.accepted or self.ending:
            return
        samples = vad.pcm16_to_float(data)
        if self.sample_rate != vad.SAMPLE_RATE:
            samples = vad.resample(samples, self.sample_rate, vad.SAMPLE_RATE)
        for kind, segment in self.endpointer.push(samples):
            self._last_activity = time.monotonic()
            if kind == "start":
                await self._barge_in()
            else:
                self._stt_queue.put_nowait(segment)

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK_SECONDS)
            now = time.monotonic()
            if now - self._started_at > settings.voice_stream_max_call_seconds:
                await self._server_end("max_duration")
                return
            busy = (self.endpointer.in_speech or not self._stt_queue.empty()
                    or (self._response is not None and not self._response.done()))
            quiet_since = max(self._last_activity, self._playback_until)
            if (self.accepted and not self.ending and not busy
                    and now - quiet_since > settings.voice_stream_idle_timeout_seconds):
                await self._server_end("idle_timeout")
                return

    async def _server_end(self, reason: str) -> None:
        self.ending = True
        await self._send({"type": "end_call", "reason": reason})
        self.stop(reason)

    # ── أدوار الزبون ────────────────────────────────────────────────────────

    async def _barge_in(self) -> None:
        if self.ending:
            return
        generating = self._response is not None and not self._response.done()
        playing = time.monotonic() < self._playback_until
        if generating:
            self._response.cancel()
        if (generating and self._turn_audio_sent) or playing:
            self._playback_until = 0.0
            await self._send({"type": "interrupt"})

    async def _stt_worker(self) -> None:
        while True:
            segment = await self._stt_queue.get()
            try:
                text = (await self.deps.stt(segment, vad.SAMPLE_RATE) or "").strip()
            except Exception:
                logger.exception("فشل نسخ مقطع بالمكالمة %s", self.room)
                await self._send({"type": "error", "code": "stt", "message": "تعذّر نسخ كلام الزبون"})
                text = ""
            if text and not self.ending:
                self.customer_lines.append(text)
                await self._send({"type": "transcript", "role": "user", "text": text})
                self._add_user_text(text)
                self._last_activity = time.monotonic()
            # الرد بس لما يسكت الزبون وتخلص كل مقاطعه — كلامه المتقطع يصير دور واحد.
            if (self._user_turn_pending and self._stt_queue.empty()
                    and not self.endpointer.in_speech and not self.ending):
                await self._start_response()

    def _add_user_text(self, text: str) -> None:
        if self.history and self.history[-1]["role"] == "user":
            self.history[-1]["content"] += "\n" + text
        else:
            self.history.append({"role": "user", "content": text})
        self._user_turn_pending = True

    # ── رد الـ agent ────────────────────────────────────────────────────────

    async def _start_response(self, opening: bool = False) -> None:
        old = self._response
        if old is not None and not old.done():
            old.cancel()
            await asyncio.gather(old, return_exceptions=True)
        self._user_turn_pending = False
        self._response = asyncio.create_task(self._respond(opening))

    async def _respond(self, opening: bool) -> None:
        self._turn_audio_sent = False
        spoken: List[str] = []
        try:
            if opening:
                self.history.append({"role": "user", "content": OPENING_PREFIX + build_opening_instruction(self.meta)})
            # end_call ممنوعة بأول جملة (مثل ignore_on_enter بمسار LiveKit).
            tools = call_tools.openai_tools(exclude={call_tools.END_CALL} if opening else ())
            for _ in range(MAX_TOOL_ROUNDS):
                splitter = SentenceSplitter()
                tool_calls = None
                messages = [{"role": "system", "content": self.instructions}, *self.history]
                async for event in self.deps.llm(messages, tools):
                    if event.get("text"):
                        for sentence in splitter.feed(event["text"]):
                            await self._speak(sentence, spoken)
                    if event.get("tool_calls"):
                        tool_calls = event["tool_calls"]
                for sentence in splitter.flush():
                    await self._speak(sentence, spoken)
                text = " ".join(spoken)
                spoken.clear()
                if not tool_calls:
                    if text:
                        self.history.append({"role": "assistant", "content": text})
                    break
                # الأدوات تكمل وتنحفظ بالتاريخ حتى لو قاطع الزبون بنصها.
                await asyncio.shield(self._run_tools(text, tool_calls))
                if self.tools.end_requested:
                    break
            if self.tools.end_requested:
                await self._agent_end()
        except asyncio.CancelledError:
            if spoken:
                self.history.append({"role": "assistant", "content": " ".join(spoken)})
            raise
        except TtsUnavailable:
            # الزبون ما يسمع شي: ننهي بأدب بدل صمت لحد مهلة السكوت، والنتيجة
            # الاحتياطية تطلب متابعة.
            logger.error("النطق فشل %d مرات ورا بعض — إنهاء المكالمة %s",
                         MAX_CONSECUTIVE_TTS_FAILURES, self.room)
            self._fallback_summary = TTS_UNAVAILABLE_SUMMARY
            await self._server_end("tts_unavailable")
        except Exception:
            logger.exception("فشل رد الـ agent بالمكالمة %s", self.room)
            await self._send({"type": "error", "code": "llm", "message": "تعذّر توليد رد الـ agent"})

    async def _run_tools(self, text: str, tool_calls: List[dict]) -> None:
        self.history.append({"role": "assistant", "content": text or None, "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            result = await call_tools.run_tool(self.tools, name, fn.get("arguments", "{}"))
            self.history.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": result})
            if name == call_tools.SAVE_OUTCOME and self.tools.outcome is not None and not self._sent_outcome:
                self._sent_outcome = True
                await self._send({"type": "outcome", **self.tools.outcome.model_dump()})
        if self.tools.end_requested:
            self.ending = True

    async def _speak(self, sentence: str, spoken: List[str]) -> None:
        text = clean_sentence(sentence)
        if not text:
            return
        failure: Optional[BaseException] = None
        try:
            result = await self.deps.tts(text)
        except Exception as exc:
            failure, result = exc, None
        if result is None:
            self._tts_failures += 1
            logger.error("فشل نطق جملة بالمكالمة %s (%d ورا بعض): %s", self.room, self._tts_failures,
                         voice_health.describe_failure(failure))
            await self._send({"type": "error", "code": "tts", "message": "تعذّر توليد صوت الـ agent",
                              "reason": voice_health.classify_error(failure)})
            if self._tts_failures >= MAX_CONSECUTIVE_TTS_FAILURES:
                raise TtsUnavailable()
            return
        self._tts_failures = 0
        audio, rate = result
        pcm = vad.float_to_pcm16(vad.resample(np.asarray(audio, dtype=np.float32), rate, vad.SAMPLE_RATE))
        chunk = vad.SAMPLE_RATE * CHUNK_MS // 1000 * 2
        lead = settings.voice_stream_tts_lead_ms / 1000.0
        for i in range(0, len(pcm), chunk):
            piece = pcm[i:i + chunk]
            ahead = self._playback_until - time.monotonic()
            if ahead > lead:
                await asyncio.sleep(ahead - lead)
            await self._send(piece)
            self._turn_audio_sent = True
            self._playback_until = max(self._playback_until, time.monotonic()) + len(piece) / 2 / vad.SAMPLE_RATE
        spoken.append(text)
        await self._send({"type": "transcript", "role": "agent", "text": text})

    async def _agent_end(self) -> None:
        """الجملة الأخيرة انرسلت كاملة: نبلّغ التطبيق يقفل الخط، وننتظره يأكد
        (call_state ended أو يسكّر الاتصال) لحد مهلة، ثم ننهي من جهتنا."""
        self.ending = True
        await self._send({"type": "end_call", "reason": "agent"})
        remaining = max(0.0, self._playback_until - time.monotonic())
        await asyncio.sleep(remaining + END_GRACE_SECONDS)
        self.stop("agent_end")


# ---------------------------------------------------------------------------
# نقطة الدخول
# ---------------------------------------------------------------------------

def _authorized(ws: WebSocket) -> bool:
    expected = settings.voice_followup_api_key or ""
    given = ws.headers.get("x-api-key") or ws.query_params.get("api_key") or ""
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


async def _reject(ws: WebSocket, code: int, error: str, message: str, **extra) -> None:
    try:
        await ws.send_text(json.dumps({"type": "error", "code": error, "message": message, **extra},
                                      ensure_ascii=False))
        await ws.close(code=code)
    except Exception:
        pass


async def _read_start(ws: WebSocket) -> Optional[dict]:
    """أول رسالة لازم start (ping قبلها مسموح). None = الاتصال انقطع."""
    while True:
        message = await ws.receive()
        if message["type"] == "websocket.disconnect":
            return None
        try:
            msg = json.loads(message.get("text") or "")
        except json.JSONDecodeError:
            msg = None
        if isinstance(msg, dict) and msg.get("type") == "ping":
            await ws.send_text(json.dumps({"type": "pong"}))
            continue
        if not isinstance(msg, dict) or msg.get("type") != "start":
            raise ValueError('أول رسالة لازم تكون {"type":"start",...}')
        return msg


async def serve(ws: WebSocket) -> None:
    global _active_calls
    await ws.accept()
    if not _authorized(ws):
        await _reject(ws, CLOSE_UNAUTHORIZED, "unauthorized", "مفتاح API غير صحيح لخدمة المتابعة الصوتية")
        return
    try:
        msg = await asyncio.wait_for(_read_start(ws), START_TIMEOUT_SECONDS)
        if msg is None:
            return
        request = CallRequest.model_validate(msg.get("call"))
        sample_rate = msg.get("sample_rate", vad.SAMPLE_RATE)
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 48000:
            raise ValueError("sample_rate لازم عدد صحيح بين 8000 و48000")
    except asyncio.TimeoutError:
        await _reject(ws, CLOSE_TIMEOUT, "timeout", "ما وصلت رسالة start بالوقت")
        return
    except ValidationError as exc:
        await _reject(ws, CLOSE_VALIDATION, "validation", "جسم المكالمة غير صالح",
                      details=json.loads(exc.json(include_url=False)))
        return
    except ValueError as exc:
        await _reject(ws, CLOSE_VALIDATION, "validation", str(exc))
        return

    if _active_calls >= settings.voice_stream_max_calls:
        await _reject(ws, CLOSE_BUSY, "busy", "وصل الخادم للحد الأقصى من المكالمات المتزامنة")
        return
    _active_calls += 1
    try:
        call = StreamCall(ws, get_deps(), request, sample_rate)
        logger.info("مكالمة WebSocket %s (%s)", call.room, call.meta.reference_id or "-")
        await call.run()
    except Exception:
        logger.exception("فشل مكالمة WebSocket")
        await _reject(ws, 1011, "internal", "خلل داخلي بالمكالمة")
    finally:
        _active_calls -= 1
