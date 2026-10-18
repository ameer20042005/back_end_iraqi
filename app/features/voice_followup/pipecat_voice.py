"""Pipecat LLM -> phrase aggregation -> TTS pipeline over the existing PCM wire.

    The processors have separate queues: TTS/network pacing never stalls reading
    the LLM SSE stream. Cancellation owns and closes every pipeline worker.
    Business tools remain in StreamCall, so both transports use the same tools.
"""

import asyncio
from dataclasses import dataclass

from pipecat.frames.frames import ControlFrame, DataFrame, EndFrame, TextFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.workers.runner import WorkerRunner


@dataclass
class GenerateFrame(DataFrame):
    pass


@dataclass
class TurnCompleteFrame(ControlFrame):
    pass


@dataclass
class PhraseFrame(DataFrame):
    text: str

    def __str__(self):
        return self.name  # Never include speech in framework diagnostics.


@dataclass
class SpeechFrame(DataFrame):
    payload: object

    def __str__(self):
        return self.name


class Generate(FrameProcessor):
    def __init__(self, events, state, first_token):
        super().__init__()
        self.events, self.state, self.first_token = events, state, first_token

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, GenerateFrame):
            try:
                async for event in self.events:
                    if event.get("text"):
                        self.first_token()
                        await self.push_frame(TextFrame(event["text"]))
                    if event.get("tool_calls"):
                        self.state["tool_calls"] = event["tool_calls"]
            except Exception as exc:
                self.state["error"] = exc
            await self.push_frame(TurnCompleteFrame())
        else:
            await self.push_frame(frame, direction)


class Aggregate(FrameProcessor):
    def __init__(self, splitter, state):
        super().__init__()
        self.splitter, self.state = splitter, state

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TextFrame):
            if self.state.get("error") is None:
                for phrase in self.splitter.feed(frame.text):
                    await self.push_frame(PhraseFrame(phrase))
        else:
            if isinstance(frame, TurnCompleteFrame) and self.state.get("error") is None:
                for phrase in self.splitter.flush():
                    await self.push_frame(PhraseFrame(phrase))
            await self.push_frame(frame, direction)


class Speak(FrameProcessor):
    def __init__(self, speak, state):
        super().__init__()
        self.speak, self.state = speak, state

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, PhraseFrame):
            if self.state.get("error") is None:
                try:
                    payload = await self.speak(frame.text)
                    if payload is not None:
                        await self.push_frame(SpeechFrame(payload))
                except Exception as exc:
                    self.state["error"] = exc
        else:
            await self.push_frame(frame, direction)


class Playback(FrameProcessor):
    def __init__(self, emit, state):
        super().__init__()
        self.emit, self.state = emit, state

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, SpeechFrame):
            if self.state.get("error") is None:
                try:
                    await self.emit(frame.payload)
                except Exception as exc:
                    self.state["error"] = exc
        else:
            if isinstance(frame, TurnCompleteFrame):
                self.state["completed"].set()
            await self.push_frame(frame, direction)


async def speak_turn(events, splitter, synthesize, emit, first_token=lambda: None):
    state = {"tool_calls": None, "error": None, "completed": asyncio.Event(), "framework_error": False}
    pipeline = Pipeline([Generate(events, state, first_token), Aggregate(splitter, state),
                         Speak(synthesize, state), Playback(emit, state)])
    worker = PipelineWorker(pipeline, enable_rtvi=False, enable_turn_tracking=False,
                            idle_timeout_secs=None, params=PipelineParams(
                                audio_in_sample_rate=16000, audio_out_sample_rate=16000))
    runner = WorkerRunner(handle_sigint=False, handle_sigterm=False)

    @worker.event_handler("on_pipeline_error")
    async def on_pipeline_error(_, frame):
        state["error"] = RuntimeError("pipecat_pipeline_error")
        state["framework_error"] = True
        state["completed"].set()
        await runner.cancel(reason="processor_error")

    await runner.add_workers(worker)
    await worker.queue_frame(GenerateFrame())
    task = asyncio.create_task(runner.run())
    try:
        await state["completed"].wait()
        # EndFrame makes Pipecat drain uninterruptibly. Queue it only when the
        # model and playback have finished, so barge-in can always cancel them.
        if not state["framework_error"]:
            await worker.queue_frame(EndFrame())
        await asyncio.shield(task)
    except asyncio.CancelledError:
        await runner.cancel(reason="interrupted")
        await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        await events.aclose()
    if state["error"] is not None:
        raise state["error"]
    return state["tool_calls"]
