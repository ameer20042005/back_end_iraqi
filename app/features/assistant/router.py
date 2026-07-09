# -*- coding: utf-8 -*-
"""مساعد عام: POST /chat (رد كامل) و POST /chat/stream (بث SSE)."""

import json
import uuid
from typing import List, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import sessions
from app.config import settings
from app.engine import llm_engine
from app.features.assistant.prompts import build_prompt
from app.rag import search

router = APIRouter(tags=["assistant"])

_SESSION_PREFIX = "assistant:"


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: List[dict]
    engine: str


def _fallback_answer(message: str, rag_results: List[dict]) -> str:
    """يُستخدم فقط إذا لم يكن vLLM متوفراً (محلياً بدون GPU)."""
    if rag_results:
        top = rag_results[0]
        if top.get("word"):
            return f"[وضع محلي بدون GPU] أقرب مطابقة RAG: {top['word']} — {top['meaning']}"
        return f"[وضع محلي بدون GPU] أقرب مطابقة RAG: {top['text']}"
    return f"[وضع محلي بدون GPU] echo: {message}"


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)
    rag_results = search(req.message, top_k=settings.rag_top_k)
    messages = build_prompt(history, req.message, rag_results)

    if llm_engine.ready:
        prompt = llm_engine.render_prompt(messages)
        answer = await llm_engine.generate_full(
            prompt, max_tokens=req.max_tokens, temperature=req.temperature
        )
        engine_name = "vllm"
    else:
        answer = _fallback_answer(req.message, rag_results)
        engine_name = "fallback"

    sessions.append(key, "user", req.message)
    sessions.append(key, "assistant", answer)

    return ChatResponse(
        session_id=session_id, answer=answer, sources=rag_results, engine=engine_name
    )


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)
    rag_results = search(req.message, top_k=settings.rag_top_k)
    messages = build_prompt(history, req.message, rag_results)

    async def event_source():
        collected = []
        if llm_engine.ready:
            prompt = llm_engine.render_prompt(messages)
            async for delta in llm_engine.generate_stream(
                prompt, max_tokens=req.max_tokens, temperature=req.temperature
            ):
                collected.append(delta)
                yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
        else:
            answer = _fallback_answer(req.message, rag_results)
            collected.append(answer)
            yield f"data: {json.dumps({'delta': answer}, ensure_ascii=False)}\n\n"

        answer = "".join(collected)
        sessions.append(key, "user", req.message)
        sessions.append(key, "assistant", answer)

        yield "data: " + json.dumps(
            {"done": True, "session_id": session_id, "sources": rag_results},
            ensure_ascii=False,
        ) + "\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")
