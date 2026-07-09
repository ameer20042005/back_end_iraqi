# -*- coding: utf-8 -*-
"""
غلاف حول vLLM AsyncLLMEngine: Continuous Batching + PagedAttention + Prefix
Caching مدمجة في vLLM نفسه (تُفعَّل عبر AsyncEngineArgs). محرك FlashAttention
يُختار تلقائياً من vLLM حسب العتاد إن كان متوفراً.

غير متوفر محلياً على Windows بدون GPU — عندها يبقى `ready = False` والباك اند
يرجع لوضع RAG-only (بدون توليد نموذج) حتى يشتغل الكود فعلياً على RunPod.
"""

import os
import uuid
from typing import AsyncGenerator, List, Optional

from app.config import settings

try:
    from huggingface_hub import snapshot_download
    from vllm import AsyncEngineArgs, AsyncLLMEngine, SamplingParams
    from vllm.lora.request import LoRARequest

    VLLM_AVAILABLE = True
except ImportError:
    VLLM_AVAILABLE = False


def _resolve_lora_path(path: str) -> str:
    """يرجع مساراً محلياً لمحوّل LoRA: كما هو إذا كان مساراً محلياً موجوداً،
    أو يحمّله تلقائياً من Hugging Face Hub إذا كان معرّف مستودع (namespace/name)."""
    if os.path.isdir(path):
        return path
    return snapshot_download(repo_id=path, token=settings.hf_token or None)


class LLMEngine:
    def __init__(self):
        self.engine = None
        self.lora_request = None

    @property
    def ready(self) -> bool:
        return self.engine is not None

    async def start(self) -> None:
        if not VLLM_AVAILABLE:
            return

        # يسمح لـ huggingface_hub (المستخدَمة داخلياً من transformers/vllm أيضاً)
        # بالمصادقة تلقائياً عند تحميل موديل بوابة (gated) مثل Gemma أو مستودع خاص.
        if settings.hf_token:
            os.environ.setdefault("HF_TOKEN", settings.hf_token)

        lora_local_path = _resolve_lora_path(settings.lora_path) if settings.lora_path else None

        engine_args = AsyncEngineArgs(
            model=settings.model_name,  # يُنزَّل تلقائياً من HF Hub إذا لم يكن مخزَّناً محلياً
            dtype=settings.dtype,
            quantization=settings.quantization,
            gpu_memory_utilization=settings.gpu_memory_utilization,
            max_model_len=settings.max_model_len,
            max_num_seqs=settings.max_num_seqs,
            enable_prefix_caching=settings.enable_prefix_caching,
            enable_lora=bool(lora_local_path),
            max_lora_rank=settings.lora_rank if lora_local_path else None,
        )
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)

        if lora_local_path:
            self.lora_request = LoRARequest("iraqi-lora", 1, lora_local_path)

    async def generate_stream(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None,
    ) -> AsyncGenerator[str, None]:
        """يبث الفروقات (delta) نصياً أولاً بأول — لإظهار بداية الرد بسرعة."""
        sampling_params = SamplingParams(
            max_tokens=max_tokens or settings.max_new_tokens,
            temperature=temperature if temperature is not None else settings.temperature,
            stop=stop or ["<|im_end|>"],
        )
        request_id = str(uuid.uuid4())
        results_generator = self.engine.generate(
            prompt, sampling_params, request_id, lora_request=self.lora_request
        )

        previous_text = ""
        async for request_output in results_generator:
            text = request_output.outputs[0].text
            delta = text[len(previous_text):]
            previous_text = text
            if delta:
                yield delta

    async def generate_full(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None,
    ) -> str:
        chunks = []
        async for delta in self.generate_stream(prompt, max_tokens, temperature, stop):
            chunks.append(delta)
        return "".join(chunks)


llm_engine = LLMEngine()
