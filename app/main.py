"""FastAPI backend — يعمل محلياً وعلى RunPod (قالب PyTorch) مع vLLM + RAG."""

import json
import uuid
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import sessions
from app.config import settings
from app.engine import VLLM_AVAILABLE, llm_engine
from app.prompts import build_prompt
from app.rag import search

try:
    import torch

    TORCH_AVAILABLE = True
except ImportError:
    # محلياً بدون torch — على RunPod تكون المكتبة موجودة في الصورة
    TORCH_AVAILABLE = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # يشغّل vLLM AsyncLLMEngine مرة واحدة عند الإقلاع (Continuous Batching +
    # PagedAttention + Prefix Caching مفعّلة عبر app/config.py).
    # محلياً بدون GPU/vLLM يبقى llm_engine.ready == False والباك اند يرجع
    # لوضع RAG-only بدل توليد النموذج.
    await llm_engine.start()
    yield


app = FastAPI(
    title="Iraqi Backend API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


@app.get("/")
def root():
    return {"status": "ok", "service": "back_end_iraqi", "docs": "/docs"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.get("/gpu")
def gpu_info():
    """معلومات الـ GPU — للتأكد أن CUDA شغالة على RunPod."""
    if not TORCH_AVAILABLE:
        return {"torch": None, "cuda": False, "note": "torch غير مثبت محلياً"}
    info = {
        "torch": torch.__version__,
        "cuda": torch.cuda.is_available(),
        "vllm_ready": llm_engine.ready,
    }
    if torch.cuda.is_available():
        info["device_count"] = torch.cuda.device_count()
        info["device_name"] = torch.cuda.get_device_name(0)
        info["cuda_version"] = torch.version.cuda
        free, total = torch.cuda.mem_get_info(0)
        info["vram_total_gb"] = round(total / 1024**3, 2)
        info["vram_free_gb"] = round(free / 1024**3, 2)
    return info


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    """رد كامل (بدون بث) — يدمج نتائج RAG في البرومبت قبل التوليد."""
    session_id = req.session_id or str(uuid.uuid4())
    history = sessions.get(session_id)
    rag_results = search(req.message, top_k=settings.rag_top_k)
    prompt = build_prompt(history, req.message, rag_results)

    if llm_engine.ready:
        answer = await llm_engine.generate_full(
            prompt, max_tokens=req.max_tokens, temperature=req.temperature
        )
        engine_name = "vllm"
    else:
        answer = _fallback_answer(req.message, rag_results)
        engine_name = "fallback"

    sessions.append(session_id, "user", req.message)
    sessions.append(session_id, "assistant", answer)

    return ChatResponse(
        session_id=session_id, answer=answer, sources=rag_results, engine=engine_name
    )


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest):
    """بث Server-Sent Events — يظهر أول جزء من الرد بسرعة بدل انتظار النص كاملاً."""
    session_id = req.session_id or str(uuid.uuid4())
    history = sessions.get(session_id)
    rag_results = search(req.message, top_k=settings.rag_top_k)
    prompt = build_prompt(history, req.message, rag_results)

    async def event_source():
        collected = []
        if llm_engine.ready:
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
        sessions.append(session_id, "user", req.message)
        sessions.append(session_id, "assistant", answer)

        yield "data: " + json.dumps(
            {"done": True, "session_id": session_id, "sources": rag_results},
            ensure_ascii=False,
        ) + "\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")
