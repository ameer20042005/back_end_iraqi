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
from typing import AsyncGenerator, Dict, List, Optional

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
        self.tokenizer = None

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

        try:
            self.tokenizer = await self.engine.get_tokenizer()
        except Exception:
            self.tokenizer = None

    def render_prompt(self, messages: List[Dict[str, str]], tools: Optional[List[dict]] = None) -> str:
        """يحوّل قائمة رسائل (system/user/assistant) لنص برومبت باستخدام قالب
        المحادثة الحقيقي (chat template) للموديل المحمَّل فعلياً — بدل قالب ثابت
        مكتوب يدوياً، حتى يبقى صحيحاً بغض النظر عن MODEL_NAME (Gemma، Qwen...).

        `tools`: تعريفات أدوات بصيغة JSON schema (اختياري) تُمرَّر لقالب
        المحادثة إن كان الموديل يدعم native function-calling؛ نحن لا نعتمد
        عليها فعلياً (انظر app/tool_loop.py) لكن تمريرها غير مكلف إن دعمها القالب.
        """
        if self.tokenizer is not None:
            kwargs = {"tokenize": False, "add_generation_prompt": True}
            if tools:
                kwargs["tools"] = tools
            return self.tokenizer.apply_chat_template(messages, **kwargs)
        # احتياطي بدائي (محلياً بدون GPU/tokenizer) — نص بسيط يكفي فقط لوضع fallback
        lines = [f"{m['role']}: {m['content']}" for m in messages]
        return "\n".join(lines) + "\nassistant:"

    async def generate_stream(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None,
        guided_json: Optional[dict] = None,
        result_holder: Optional[dict] = None,
    ) -> AsyncGenerator[str, None]:
        """يبث الفروقات (delta) نصياً أولاً بأول — لإظهار بداية الرد بسرعة.

        `result_holder`: إن مُرِّر (dict فارغ)، يُعبَّأ بعد انتهاء التوليد بـ
        `stop_reason`/`finish_reason` — تُستخدم لمعرفة أي stop-string أوقف
        التوليد (مثل [ORDER_READY]) دون تسريب النص نفسه للعميل.
        """
        sampling_kwargs = dict(
            max_tokens=max_tokens or settings.max_new_tokens,
            temperature=temperature if temperature is not None else settings.temperature,
        )
        if stop:
            sampling_kwargs["stop"] = stop
        if guided_json is not None:
            try:
                from vllm.sampling_params import GuidedDecodingParams

                sampling_kwargs["guided_decoding"] = GuidedDecodingParams(json=guided_json)
            except ImportError:
                pass  # نسخة أقدم من vLLM بدون guided decoding — نعتمد على وصف المخطط بالبرومبت فقط

        sampling_params = SamplingParams(**sampling_kwargs)
        request_id = str(uuid.uuid4())
        results_generator = self.engine.generate(
            prompt, sampling_params, request_id, lora_request=self.lora_request
        )

        previous_text = ""
        last_output = None
        async for request_output in results_generator:
            last_output = request_output
            text = request_output.outputs[0].text
            delta = text[len(previous_text):]
            previous_text = text
            if delta:
                yield delta

        if result_holder is not None and last_output is not None:
            result_holder["stop_reason"] = getattr(last_output.outputs[0], "stop_reason", None)
            result_holder["finish_reason"] = getattr(last_output.outputs[0], "finish_reason", None)

    async def generate_full(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None,
        guided_json: Optional[dict] = None,
        result_holder: Optional[dict] = None,
    ) -> str:
        chunks = []
        async for delta in self.generate_stream(
            prompt, max_tokens, temperature, stop, guided_json, result_holder
        ):
            chunks.append(delta)
        return "".join(chunks)


llm_engine = LLMEngine()
