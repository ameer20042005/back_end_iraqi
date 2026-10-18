"""Local contract server for cc-app::pipecat_backend_round_trip.

Uses the real FastAPI/Pipecat pipeline, deterministic fake LLM/TTS, no external
services, a temporary outcome directory, and exits after one WebSocket call.
"""

import json
import socket
import tempfile
from dataclasses import replace
from pathlib import Path

import numpy as np
import uvicorn

from app import main
from app.features.voice_followup import gateway, outcomes, stream, vad


async def llm(messages, tools):
    if any(isinstance(m.get("content"), list) for m in messages):
        yield {"text": "<customer_transcript>اي وصل</customer_transcript>تمام، شكراً إلك."}
        yield {"tool_calls": [
            {"id": "save", "type": "function", "function": {
                "name": "save_outcome", "arguments": json.dumps({"summary": "تم التحقق"})}},
            {"id": "end", "type": "function", "function": {"name": "end_call", "arguments": "{}"}},
        ]}
    else:
        yield {"text": "مرحبا بيك، هل وصلك الطلب؟"}


async def tts(text):
    return np.full(2400, 0.1, dtype=np.float32), 24000


class LocalSubmitter:
    async def submit(self, *args):
        return True


def main_server():
    config = replace(stream.settings, voice_followup_api_key="test-key-123", voice_stream_vad="energy",
                     voice_stream_min_speech_ms=100, voice_stream_end_silence_ms=200)
    stream.settings = vad.settings = config
    stream.deps = stream.StreamDeps(llm=llm, tts=tts, vad=vad.make_classifier, prewarm_opening=True)
    gateway.call_outcome_submitter = LocalSubmitter()
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(main.app, lifespan="off", log_level="warning"))
    original_serve = stream.serve

    async def serve_once(ws):
        try:
            await original_serve(ws)
        finally:
            server.should_exit = True

    stream.serve = serve_once
    with tempfile.TemporaryDirectory(prefix="pipecat-contract-") as data:
        outcomes.settings = replace(outcomes.settings, call_outcomes_dir=Path(data))
        print(f"PIPECAT_TEST_URL=ws://127.0.0.1:{port}/voice_followup/stream", flush=True)
        server.run(sockets=[sock])


if __name__ == "__main__":
    main_server()
