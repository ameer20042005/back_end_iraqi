import asyncio
import json

import httpx
import numpy as np
import pytest

from app.core.engine import llm_engine
from app.features.voice_followup import audio_input, stream


def test_native_audio_reaches_vllm_without_asr_or_thinking(monkeypatch):
    body = {}

    def handle(request):
        body.update(json.loads(request.content))
        chunks = [
            {"choices": [{"delta": {"content": "<customer_transcript>اي</customer_transcript>تمام."}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        return httpx.Response(200, text="\n".join("data: " + json.dumps(c) + "\n" for c in chunks) + "data: [DONE]\n")

    async def check():
        async with httpx.AsyncClient(base_url="http://model.test/v1", transport=httpx.MockTransport(handle)) as client:
            monkeypatch.setattr(llm_engine, "_client", client)
            monkeypatch.setattr(llm_engine, "_ready", True)
            result = [e async for e in stream.vllm_stream([
                {"role": "user", "content": audio_input.audio_content(np.zeros(16000, dtype=np.float32))}], [])]
            assert result[0]["text"].startswith("<customer_transcript>")

    asyncio.run(check())
    assert body["messages"][0]["content"][0]["type"] == "input_audio"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["max_tokens"] == stream.settings.voice_stream_llm_max_tokens + 256


@pytest.mark.parametrize("response, error", [
    ('data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n', "llm_output_truncated"),
    ('data: {"choices":[{"delta":{"content":"partial"}}]}\n', "llm_stream_incomplete"),
    ('data: {broken json}\n', "invalid_llm_stream_json"),
])
def test_broken_or_truncated_model_streams_are_explicit(monkeypatch, response, error):
    async def check():
        async with httpx.AsyncClient(base_url="http://model.test", transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text=response))) as client:
            monkeypatch.setattr(llm_engine, "_client", client)
            monkeypatch.setattr(llm_engine, "_ready", True)
            with pytest.raises(ValueError, match=error):
                _ = [e async for e in stream.vllm_stream([], [])]

    asyncio.run(check())
