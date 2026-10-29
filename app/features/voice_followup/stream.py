# -*- coding: utf-8 -*-
"""Whisper -> Qwen -> Pipecat/F5-TTS over the WhatsApp PCM WebSocket contract.

Whisper transcribes once; Qwen receives text and manages the dialogue and tools.
Pipecat pipelines LLM tokens, phrase aggregation, F5-TTS and paced PCM.
Only accepted WhatsApp calls receive speech; opening synthesis can run while ringing.
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
from app.features.voice_followup import call_stats, call_tools, vad, voice_health
from app.features.voice_followup.calls import agent_metadata, room_name, consume_stream_id
from app.features.voice_followup.prompts import build_call_instructions, build_opening_instruction
from app.features.voice_followup.schema import CallRequest
from app.features.voice_followup import stt, pipecat_voice

logger = logging.getLogger(__name__)

# رموز إغلاق WebSocket الخاصة بهذا العقد (نطاق 4000-4999 للتطبيقات).
CLOSE_UNAUTHORIZED = 4401
CLOSE_TIMEOUT = 4408
CLOSE_VALIDATION = 4422
CLOSE_BUSY = 4429
CLOSE_UNAVAILABLE = 1013

START_TIMEOUT_SECONDS = 15.0
# بعد end_call: مهلة للتطبيق حتى يكمّل تشغيل الجملة الأخيرة ويقفل الخط.
END_GRACE_SECONDS = 10.0
# صوت الـ agent ينبعث بقطع 40ms حتى يقدر التطبيق يوقفه بسرعة عند المقاطعة.
CHUNK_MS = 40
MAX_TOOL_ROUNDS = 4
WATCHDOG_TICK_SECONDS = 0.5
# كل كم ثانية تنكتب إحصاءات صوت الزبون باللوق وبـ /health/voice (last_call).
STATS_LOG_SECONDS = 5.0
SEND_TIMEOUT_SECONDS = 5.0

OPENING_PREFIX = "(تعليمات داخلية من النظام، مو كلام الزبون) "

# فشل النطق هذا العدد من المرات ورا بعض بنفس المكالمة = الزبون بصمت، فننهيها.
MAX_CONSECUTIVE_TTS_FAILURES = 2
TTS_UNAVAILABLE_SUMMARY = "تعذّر توليد صوت الـ agent فانتهت المكالمة — تحتاج متابعة"

# ---------------------------------------------------------------------------
# التبعيات الثقيلة (قابلة للتبديل بالاختبارات)
# ---------------------------------------------------------------------------

# يرجع أحداث {"text": "..."} أثناء التوليد، و{"tool_calls": [...]} مرة بالنهاية.
LlmFn = Callable[[List[dict], List[dict]], AsyncIterator[Dict[str, Any]]]
TtsFn = Callable[[str], Awaitable[Optional[Tuple[np.ndarray, int]]]]
SttFn = Callable[[np.ndarray, int], Awaitable[str]]


@dataclass
class StreamDeps:
    llm: LlmFn
    tts: TtsFn
    vad: Callable[[], Any]
    prewarm_opening: bool = False
    stt: SttFn = stt.transcribe


async def f5_tts(text: str) -> Optional[Tuple[np.ndarray, int]]:
    from app.features.voice_followup import tts as speech

    result = await asyncio.to_thread(speech.synthesize_array, text)
    if result is None:
        raise voice_health.NotInstalledError("موديل النطق غير مثبَّت (f5-tts)")
    return result


async def vllm_stream(messages: List[dict], tools: List[dict]) -> AsyncIterator[Dict[str, Any]]:
    """خادم vLLM المتعدد الوسائط: بلا تفكير،
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
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    calls: Dict[int, Dict[str, str]] = {}
    completed = False
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
                raise ValueError("invalid_llm_stream_json") from None
            for choice in chunk.get("choices") or []:
                if choice.get("finish_reason") == "length":
                    raise ValueError("llm_output_truncated")
                if choice.get("finish_reason") in ("stop", "tool_calls"):
                    completed = True
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
    if not completed:
        raise ValueError("llm_stream_incomplete")
    if calls:
        yield {"tool_calls": [
            {"id": s["id"] or f"call_{uuid.uuid4().hex}", "type": "function",
             "function": {"name": s["name"], "arguments": s["arguments"] or "{}"}}
            for _, s in sorted(calls.items()) if s["name"]
        ]}


def default_deps() -> StreamDeps:
    return StreamDeps(llm=vllm_stream, tts=f5_tts, vad=vad.make_classifier, prewarm_opening=True)


# None = التبعيات الحقيقية؛ الاختبارات تضبطها بنسخ وهمية.
deps: Optional[StreamDeps] = None


def get_deps() -> StreamDeps:
    return deps or default_deps()


# ---------------------------------------------------------------------------
# تقطيع رد النموذج لجمل
# ---------------------------------------------------------------------------

_SENTENCE_END = re.compile(r"\.(?!\d)|[!?؟؛…\n]+")
_SOFT_BREAK = re.compile(r"[،,:]\s|\s")


class SentenceSplitter:
    """يجمع نص النموذج المتدفق ويطلّع جمل كاملة تنطق وحدة وحدة. الجملة الأقصر
    من min_chars تنضم للي بعدها (موديل النطق ينطق الجمل الطويلة شوي أطبع)."""

    def __init__(self, min_chars: int = 8, max_chars: int = 200, first_chars: Optional[int] = None) -> None:
        self._buf = ""
        self._min = min_chars
        self._max = max_chars
        self._first = first_chars if first_chars and first_chars < max_chars else None

    def feed(self, text: str) -> List[str]:
        self._buf += text
        out = []
        while True:
            cut = None
            limit = self._first or self._max
            if self._first:
                # A tiny greeting alone forces another TTS generation before
                # the caller hears who is calling. Keep it with the introduction.
                for clause in re.finditer(r"[,،؛]", self._buf):
                    if max(self._min, 24) <= clause.end() <= limit:
                        cut = clause.end()
                        break
            for m in _SENTENCE_END.finditer(self._buf):
                if (m.group() == "." and m.end() == len(self._buf)
                        and m.start() > 0 and self._buf[m.start() - 1].isdigit()):
                    continue  # A decimal may arrive in the next token.
                if cut is None and m.end() <= limit and len(self._buf[:m.end()].strip()) >= self._min:
                    cut = m.end()
                    break
            if cut is None and len(self._buf) > limit:
                breaks = [m.end() for m in _SOFT_BREAK.finditer(self._buf, 0, limit + 1)]
                # Do not synthesize half a word when its token arrives in pieces.
                # Wait for a boundary, with a hard cap for malformed long tokens.
                if breaks:
                    cut = breaks[-1]
                else:
                    boundary = _SOFT_BREAK.search(self._buf, limit, self._max)
                    cut = boundary.end() if boundary else (self._max if len(self._buf) > self._max else None)
            if cut is None:
                return out
            sentence = self._buf[:cut].strip()
            self._buf = self._buf[cut:]
            if sentence:
                out.append(sentence)
                self._first = None

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
    def __init__(self, ws: WebSocket, deps_: StreamDeps, request: CallRequest, sample_rate: int,
                 reserved_room: Optional[str] = None) -> None:
        self.ws = ws
        self.deps = deps_
        self.sample_rate = sample_rate
        self.meta = agent_metadata(request)  # بلا اسم الزبون
        self.room = reserved_room or room_name(request)
        self.trace_id = uuid.uuid4().hex[:12]
        self.instructions = build_call_instructions(self.meta, call_tools.data_tools_enabled())
        self.history: List[dict] = []
        self.customer_lines: List[str] = []
        self.tools = call_tools.CallToolContext(
            room=self.room, meta=self.meta, customer_transcript=self.customer_transcript,
        )
        classifier = deps_.vad()
        self.endpointer = vad.Endpointer(classifier)
        self.agc = vad.make_agc()
        self.stats = call_stats.CallAudioStats(
            sample_rate, getattr(classifier, "kind", type(classifier).__name__), self.agc is not None)
        self._odd_byte = b""

        self.accepted = False
        self._accepted_event = asyncio.Event()
        self._accepted_at: Optional[float] = None
        self.ending = False          # end_call انطلب: ما نقبل أدوار جديدة ولا مقاطعة
        self.disconnected = False
        self.end_reason = ""
        self._stop = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._audio_queue: "asyncio.Queue[np.ndarray]" = asyncio.Queue(maxsize=8)
        self._response: Optional[asyncio.Task] = None
        self._turn_audio_sent = False
        self._playback_until = 0.0
        self._started_at = time.monotonic()
        self._last_activity = self._started_at
        self._sent_outcome = False
        self._tts_failures = 0
        self._fallback_summary: Optional[str] = None
        self.input_mode = "whisper"
        self._transcribing = False
        self._input_revision = 0
        self._stt_failures = 0
        self._stt_ms = 0
        self._pending_transcript = False
        self._turn_started = self._started_at
        self._token_ms: Optional[int] = None
        self._first_sent_ms: Optional[int] = None
        self._ever_sent_audio = False
        self._tts_ms = 0
        self._response_count = 0
        self._stt_turns = 0
        self._response_errors = 0
        self._turn_origin: Optional[float] = None
        self._turn_phrases = set()
        self._barge_pending = False

    # ── الإرسال ─────────────────────────────────────────────────────────────

    async def _send(self, payload) -> None:
        if self.disconnected:
            return

        async def do():
            async with self._send_lock:
                if self.disconnected:
                    return
                try:
                    async with asyncio.timeout(SEND_TIMEOUT_SECONDS):
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
        logger.info("voice_pipeline trace=%s engine=pipecat input=%s vad=%s sample_rate=%d",
                    self.trace_id, self.input_mode, self.stats.vad_kind, self.sample_rate)
        tasks = [asyncio.create_task(self._guard_worker(self._receive_loop)),
                 asyncio.create_task(self._guard_worker(self._audio_worker)),
                 asyncio.create_task(self._guard_worker(self._watchdog)),
                 asyncio.create_task(self._guard_worker(self._stats_loop))]
        try:
            if self.deps.prewarm_opening:
                await self._start_response(opening=True)
            await self._stop.wait()
        finally:
            pending = tasks + ([self._response] if self._response else [])
            for task in pending:
                task.cancel()
            # النتيجة الاحتياطية تنكتب قبل أي await حتى لو انلغى الاتصال نفسه
            # (إيقاف الخادم مثلاً).
            fallback = self._write_fallback()
            self._report_stats(active=False)
            await asyncio.gather(*pending, return_exceptions=True)
            await self._finalize(fallback)

    async def _guard_worker(self, operation) -> None:
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._response_errors += 1
            logger.error("voice_worker_failed trace=%s worker=%s error_type=%s",
                         self.trace_id, operation.__name__, type(exc).__name__)
            await self._send({"type": "error", "code": "voice_worker_failed",
                              "message": "توقفت معالجة المكالمة؛ راجع سجل الخادم"})
            self.stop("voice_worker_failed")

    def _report_stats(self, active: bool) -> None:
        self.stats.on_vad(self.endpointer.windows, self.endpointer.speech_windows, self.endpointer.starts)
        logger.info("إحصاءات صوت المكالمة %s%s: %s", self.trace_id, "" if active else " (النهاية)",
                    self.stats.log_line(active))
        voice_health.record_last_call(self.stats.snapshot(active))

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_LOG_SECONDS)
            self._report_stats(active=True)

    def _write_fallback(self):
        if self.tools.saved:
            return None
        summary = self._fallback_summary or (
            call_tools.NO_OUTCOME_SUMMARY if self.accepted else call_tools.NO_ANSWER_SUMMARY)
        return call_tools.save_fallback_outcome(self.meta, self.room, summary, self.customer_transcript())

    async def _finalize(self, fallback) -> None:
        if fallback is not None:
            await self._send({"type": "outcome", **fallback.model_dump()})
        logger.info("انتهت مكالمة WebSocket %s (%s)", self.trace_id, self.end_reason or "cancelled")
        logger.info("voice_call_end trace=%s engine=pipecat input=%s reason=%s duration_ms=%d responses=%d "
                    "stt_turns=%d response_errors=%d interrupts=%d", self.trace_id, self.input_mode,
                    self.end_reason or "cancelled", int((time.monotonic() - self._started_at) * 1000),
                    self._response_count, self._stt_turns, self._response_errors, self.stats.interrupts)
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
            self.stats.protocol_errors += 1
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
                self._turn_origin = self._last_activity
                self._accepted_at = self._last_activity
                self._accepted_event.set()
                if not self.deps.prewarm_opening:
                    await self._start_response(opening=True)
            elif state == "ended":
                self.stop("customer_hangup")
            elif state not in ("ringing", "accepted"):
                self.stats.protocol_errors += 1
                await self._send({"type": "error", "code": "protocol", "message": f"حالة غير معروفة: {state!r}"})
        else:
            self.stats.protocol_errors += 1
            await self._send({"type": "error", "code": "protocol", "message": f"نوع رسالة غير معروف: {kind!r}"})

    async def _on_audio(self, data: bytes) -> None:
        # الصوت يتعالج دائماً وقت ما الـ agent يحچي (للنسخ وللمقاطعة سوية)؛
        # يتجاهل بس قبل accepted وبعد end_call.
        if not self.accepted or self.ending:
            self.stats.on_ignored(len(data))
            return
        # قطعة بعدد بايتات فردي: البايت الأخير يكمّل أول القطعة الجاية بدل ما
        # ينرمى ويزيح كل العينات اللي بعده بايت.
        data = self._odd_byte + data
        cut = len(data) - len(data) % 2
        data, self._odd_byte = data[:cut], data[cut:]
        samples = vad.pcm16_to_float(data)
        if self.sample_rate != vad.SAMPLE_RATE:
            samples = vad.resample(samples, self.sample_rate, vad.SAMPLE_RATE)
        self.stats.on_audio(len(data), samples)
        if self.agc is not None:
            samples = self.agc.process(samples)
            self.stats.on_gain(self.agc.gain)
        for kind, segment in self.endpointer.push(samples):
            self._last_activity = time.monotonic()
            if kind == "start":
                # أثناء نطق الـ agent نؤجل المقاطعة حتى يثبت أنه كلام مقصود.
                if self._agent_speaking():
                    self._barge_pending = True
                else:
                    await self._barge_in()
            else:
                if self._barge_pending:
                    self._barge_pending = False
                    if self._agent_speaking(margin=1.0):
                        # كلمة قصيرة أو صدى أو ضوضاء والـ agent ما زال يتكلم:
                        # نتجاهلها بدل قطع كلامه وإعادة الدور.
                        self.stats.ignored_short += 1
                        continue
                self.stats.on_segment(segment.size)
                self._turn_origin = time.monotonic()
                try:
                    self._audio_queue.put_nowait(segment)
                except asyncio.QueueFull:
                    self._response_errors += 1
                    await self._send({"type": "error", "code": "audio_backpressure",
                                      "message": "الخادم لا يستطيع معالجة الصوت بالسرعة المطلوبة"})
                    await self._server_end("audio_backpressure")
                    return
        if (self._barge_pending and self.endpointer.in_speech
                and self.endpointer.speech_ms >= settings.voice_stream_barge_in_ms):
            self._barge_pending = False
            await self._barge_in()

    def _agent_speaking(self, margin: float = 0.0) -> bool:
        """صوت الـ agent ما زال يُشغَّل عند الزبون (لأكثر من margin ثانية)."""
        return self._playback_until - time.monotonic() > margin

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_TICK_SECONDS)
            now = time.monotonic()
            if now - self._started_at > settings.voice_stream_max_call_seconds:
                await self._server_end("max_duration")
                return
            busy = (self._transcribing or self.endpointer.in_speech or not self._audio_queue.empty()
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
        self._input_revision += 1
        generating = self._response is not None and not self._response.done()
        playing = time.monotonic() < self._playback_until
        if generating and not self._response.cancelling():
            self._response.cancel()
        if (generating and self._turn_audio_sent) or playing:
            self._playback_until = 0.0
            self.stats.interrupts += 1
            await self._send({"type": "interrupt"})

    async def _audio_worker(self) -> None:
        while True:
            segment = await self._audio_queue.get()
            if self.ending:
                continue
            await self._cancel_response()
            if self.ending or self._stop.is_set():
                continue
            revision = self._input_revision
            self._transcribing = True
            started = time.monotonic()
            try:
                transcript = (await self.deps.stt(segment, vad.SAMPLE_RATE)).strip()
                self._stt_failures = 0
                self.stats.on_transcript(transcript)
            except Exception as exc:
                self.stats.on_stt_error(exc)
                self._stt_failures += 1
                self._response_errors += 1
                await self._send({"type": "error", "code": "stt",
                                  "message": "تعذّر فهم المقطع الصوتي", "reason": voice_health.classify_error(exc)})
                if self._stt_failures >= 2:
                    await self._server_end("stt_unavailable")
                continue
            finally:
                self._transcribing = False
                self._stt_ms = int((time.monotonic() - started) * 1000)
                self._last_activity = time.monotonic()
            self._stt_turns += 1
            if transcript:
                self._pending_transcript = True
                self.customer_lines.append(transcript)
                self.history.append({"role": "user", "content": transcript})
                await self._send({"type": "transcript", "role": "user", "text": transcript})
            if (self._pending_transcript and revision == self._input_revision and self._audio_queue.empty()
                    and not self.endpointer.in_speech and not self.ending):
                self._pending_transcript = False
                await self._start_response()

    # ── رد الـ agent ────────────────────────────────────────────────────────

    async def _cancel_response(self) -> None:
        old = self._response
        if old is not None and not old.done():
            if not old.cancelling():
                old.cancel()
            await asyncio.gather(old, return_exceptions=True)

    async def _start_response(self, opening: bool = False) -> None:
        await self._cancel_response()
        self._response = asyncio.create_task(self._respond(opening))

    async def _respond(self, opening: bool) -> None:
        self._turn_audio_sent = False
        self._turn_phrases.clear()
        spoken: List[str] = []
        self._turn_started = self._turn_origin or time.monotonic()
        self._token_ms, self._first_sent_ms, self._tts_ms = None, None, 0
        self._response_count += 1
        status = "completed"
        try:
            if opening:
                self.history.append({"role": "user", "content": OPENING_PREFIX + build_opening_instruction(self.meta)})
            # Opening cannot save invented answers or end a call before listening.
            tools = [] if opening else call_tools.openai_tools()
            for _ in range(MAX_TOOL_ROUNDS):
                messages = [{"role": "system", "content": self.instructions}, *self.history]
                tool_calls = await pipecat_voice.speak_turn(
                    self._model_events(messages, tools),
                    SentenceSplitter(first_chars=settings.voice_stream_first_phrase_chars),
                    self._synthesize, lambda payload: self._play_speech(payload, spoken), self._first_token)
                text = " ".join(spoken)
                spoken.clear()
                if not tool_calls:
                    if text:
                        self.history.append({"role": "assistant", "content": text})
                    break
                # الأدوات تكمل وتنحفظ بالتاريخ حتى لو قاطع الزبون بنصها.
                await self._accepted_event.wait()
                tool_task = asyncio.create_task(self._run_tools(text, tool_calls))
                try:
                    await asyncio.shield(tool_task)
                except asyncio.CancelledError:
                    # Own the shielded task: a barge-in during persistence must
                    # not orphan a save or leave a completed call listening.
                    await tool_task
                    if not self._stop.is_set() and not self.disconnected and (self.tools.saved or self.tools.end_requested):
                        await self._finish_call(text, spoken)
                    raise
                if self.tools.saved or self.tools.end_requested:
                    await self._finish_call(text, spoken)
                    break
        except asyncio.CancelledError:
            status = "interrupted"
            if spoken:
                self.history.append({"role": "assistant", "content": " ".join(spoken)})
            raise
        except TtsUnavailable:
            status = "tts_unavailable"
            self._response_errors += 1
            # الزبون ما يسمع شي: ننهي بأدب بدل صمت لحد مهلة السكوت، والنتيجة
            # الاحتياطية تطلب متابعة.
            logger.error("النطق فشل %d مرات ورا بعض — إنهاء المكالمة %s",
                         MAX_CONSECUTIVE_TTS_FAILURES, self.room)
            self._fallback_summary = TTS_UNAVAILABLE_SUMMARY
            await self._server_end("tts_unavailable")
        except Exception as exc:
            status = "error"
            self._response_errors += 1
            logger.exception("فشل رد الـ agent بالمكالمة %s", self.room)
            await self._send({"type": "error", "code": "llm",
                              "message": "تعذّر توليد رد الـ agent"})
            if self.tools.saved or self.tools.end_requested:
                await self._agent_end()
        finally:
            logger.info("voice_turn_end trace=%s engine=pipecat input=%s opening=%s status=%s "
                        "first_token_ms=%s first_audio_ms=%s stt_ms=%d tts_ms=%d total_ms=%d",
                        self.trace_id, self.input_mode, opening, status, self._token_ms, self._first_sent_ms,
                        self._stt_ms, self._tts_ms, int((time.monotonic() - self._turn_started) * 1000))

    def _first_token(self) -> None:
        if self._token_ms is None:
            self._token_ms = int((time.monotonic() - self._turn_started) * 1000)

    async def _finish_call(self, text: str, spoken: List[str]) -> None:
        self.ending = True
        try:
            if self.tools.saved or not text:
                closing = ("Thank you for your time. Goodbye." if self.meta.language.lower().startswith("en")
                           else "شكراً لوقتك، مع السلامة.")
                payload = await self._synthesize(closing)
                if payload is not None:
                    await self._play_speech(payload, spoken)
        except Exception:
            self._response_errors += 1
            logger.exception("تعذّر نطق الوداع بالمكالمة %s", self.trace_id)
        finally:
            # A failed goodbye must never prevent hangup after a saved outcome.
            if not self.disconnected:
                await self._agent_end()

    async def _model_events(self, messages, tools):
        events = self.deps.llm(messages, tools)
        try:
            async for event in events:
                if event.get("text"):
                    self._first_token()
                yield event
        finally:
            await events.aclose()

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
        if self.tools.saved or self.tools.end_requested:
            self.ending = True

    async def _synthesize(self, sentence: str):
        text = clean_sentence(sentence)
        if not text:
            return
        key = re.sub(r"[\W_]+", "", text).casefold()
        if len(key) >= 12 and key in self._turn_phrases:
            return
        failure: Optional[BaseException] = None
        tts_started = time.monotonic()
        try:
            result = await self.deps.tts(text)
        except Exception as exc:
            failure, result = exc, None
        self._tts_ms += int((time.monotonic() - tts_started) * 1000)
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
        audio = vad.fade_edges(vad.trim_tts_padding(np.asarray(audio, dtype=np.float32), rate), rate)
        pcm = vad.float_to_pcm16(vad.resample(audio, rate, vad.SAMPLE_RATE))
        if not pcm:
            raise TtsUnavailable("empty_tts_audio")
        self._turn_phrases.add(key)
        return text, pcm

    async def _play_speech(self, payload, spoken: List[str]) -> None:
        text, pcm = payload
        # Prepare LLM and TTS while the WhatsApp leg is ringing; never speak or
        # emit a transcript until the client explicitly confirms acceptance.
        await self._accepted_event.wait()
        chunk = vad.SAMPLE_RATE * CHUNK_MS // 1000 * 2
        lead = settings.voice_stream_tts_lead_ms / 1000.0
        recorded = False
        try:
            for i in range(0, len(pcm), chunk):
                piece = pcm[i:i + chunk]
                ahead = self._playback_until - time.monotonic()
                if ahead > lead:
                    await asyncio.sleep(ahead - lead)
                await self._send(piece)
                if not recorded:
                    # Keep the introduction in history even when playback is
                    # interrupted halfway; otherwise the model greets again.
                    spoken.append(text)
                    recorded = True
                self._on_audio_sent(piece)
        except asyncio.CancelledError:
            if recorded:
                spoken[-1] += " [قوطع الكلام قبل اكتماله؛ لا تعد ما قلته بنفس الصياغة، تابع من حيث وصلت]"
            raise
        await self._send({"type": "transcript", "role": "agent", "text": text})

    def _on_audio_sent(self, piece: bytes) -> None:
        if self._first_sent_ms is None:
            self._first_sent_ms = int((time.monotonic() - self._turn_started) * 1000)
            if not self._ever_sent_audio and self._accepted_at is not None:
                logger.info("voice_first_audio trace=%s accepted_to_audio_ms=%d input=%s prewarmed=%s",
                            self.trace_id, int((time.monotonic() - self._accepted_at) * 1000),
                            self.input_mode, self.deps.prewarm_opening)
            self._ever_sent_audio = True
        self._turn_audio_sent = True
        self._playback_until = max(self._playback_until, time.monotonic()) + len(piece) / 2 / vad.SAMPLE_RATE

    async def _agent_end(self) -> None:
        """الجملة الأخيرة انرسلت كاملة: نبلّغ التطبيق يقفل الخط، وننتظره يأكد
        (call_state ended أو يسكّر الاتصال) لحد مهلة، ثم ننهي من جهتنا."""
        self.ending = True
        remaining = max(0.0, self._playback_until - time.monotonic())
        # Send hangup only after the final PCM has had time to play on the client.
        await asyncio.sleep(remaining)
        await self._send({"type": "end_call", "reason": "agent"})
        await asyncio.sleep(END_GRACE_SECONDS)
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
        stream_id = msg.get("stream_id")
        if stream_id is not None and not isinstance(stream_id, str):
            raise ValueError("stream_id must be a string")
        reserved_room = consume_stream_id(stream_id, request) if stream_id else None
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
    if deps is None:
        # Never tell Rust "ready" and dial the customer when speech cannot run.
        from app.core.engine import llm_engine
        from app.features.voice_followup import tts

        if not llm_engine.ready:
            await _reject(ws, CLOSE_UNAVAILABLE, "model_unavailable", "نموذج الصوت غير جاهز؛ أعد المحاولة بعد جاهزية الخادم")
            return
        tts_status = tts.status()
        if not tts_status["installed"]:
            await _reject(ws, CLOSE_UNAVAILABLE, "tts_unavailable", "خدمة النطق غير متوفرة على الخادم")
            return
        if voice_health.warmup_enabled() and not tts_status["loaded"]:
            await _reject(ws, CLOSE_UNAVAILABLE, "voice_warming", "الخادم يجهّز صوت الوكيل؛ أعد المحاولة بعد اكتمال التحميل")
            return
        stt_status = stt.status()
        if not stt_status["installed"]:
            await _reject(ws, CLOSE_UNAVAILABLE, "stt_unavailable", "خدمة التعرف على الصوت غير متوفرة على الخادم")
            return
        if voice_health.warmup_enabled() and not stt_status["loaded"]:
            await _reject(ws, CLOSE_UNAVAILABLE, "voice_warming", "الخادم يجهّز Whisper؛ أعد المحاولة بعد اكتمال التحميل")
            return
    _active_calls += 1
    try:
        call = StreamCall(ws, get_deps(), request, sample_rate, reserved_room)
        logger.info("voice_call_start trace=%s", call.trace_id)
        await call.run()
    except Exception:
        logger.exception("فشل مكالمة WebSocket")
        await _reject(ws, 1011, "internal", "خلل داخلي بالمكالمة")
    finally:
        _active_calls -= 1
