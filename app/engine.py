# -*- coding: utf-8 -*-
"""
غلاف حول vLLM AsyncLLMEngine: Continuous Batching + PagedAttention + Prefix
Caching مدمجة في vLLM نفسه (تُفعَّل عبر AsyncEngineArgs). محرك FlashAttention
يُختار تلقائياً من vLLM حسب العتاد إن كان متوفراً.

**موديل واحد لكل شي**: Gemma 4 عبر vLLM يدعم نص + صورة (+ صوت لاحقاً) أصلاً —
انظر `generate_full`/`generate_stream` مع `multi_modal_data`. لا نحمّل أي نسخة
ثانية من الموديل بمكان آخر بالمشروع.

غير متوفر محلياً على Windows بدون GPU — عندها يبقى `ready = False` والباك اند
يرجع لوضع RAG-only (بدون توليد نموذج) حتى يشتغل الكود فعلياً على RunPod.
"""

import os
import uuid
from typing import AsyncGenerator, Dict, List, Optional

from app.config import settings

from app.hf_utils import resolve_lora_path

try:
    from vllm import AsyncEngineArgs, AsyncLLMEngine, SamplingParams
    from vllm.lora.request import LoRARequest

    VLLM_AVAILABLE = True
except ImportError:
    VLLM_AVAILABLE = False

try:
    from transformers import AutoProcessor

    PROCESSOR_AVAILABLE = True
except ImportError:
    PROCESSOR_AVAILABLE = False


class LLMEngine:
    def __init__(self):
        self.engine = None
        self.lora_request = None
        self.tokenizer = None
        self.processor = None  # للبرومبتات متعددة الوسائط (صورة/صوت) — انظر render_multimodal_prompt
        self.extra_stop_token_ids: List[int] = []  # مثلاً <end_of_turn> لـ Gemma — انظر start()

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

        lora_local_path = (
            resolve_lora_path(settings.lora_path, settings.hf_token or None)
            if settings.lora_path else None
        )

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
            trust_remote_code=True,  # لازمة لمعمارية Gemma 4 متعددة الوسائط
            limit_mm_per_prompt={"image": 4},  # أقصى عدد صور بكل طلب (ميزة order_intake)
        )
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)

        if lora_local_path:
            self.lora_request = LoRARequest("iraqi-lora", 1, lora_local_path)

        try:
            self.tokenizer = await self.engine.get_tokenizer()
        except Exception:
            self.tokenizer = None

        # نمرر <end_of_turn> و eos_token_id الأساسي معاً كـ stop token ids —
        # بتجارب llm_iraqi_best.ipynb (خلية "كود الاستدلال الصحيح") الموديل
        # يتوقف أحياناً بـ eos الأساسي بدل <end_of_turn>، والاعتماد على واحد
        # منهم فقط يخلي التوليد يكمل بعد نهاية الرد الفعلي وينتج نصاً مشوّشاً
        # (stop_ids = [106, 1] بالضبط بتوثيق النموذج على Hugging Face).
        if self.tokenizer is not None:
            try:
                eot_id = self.tokenizer.convert_tokens_to_ids("<end_of_turn>")
                stop_ids = set()
                if isinstance(eot_id, int) and eot_id >= 0:
                    stop_ids.add(eot_id)
                if self.tokenizer.eos_token_id is not None:
                    stop_ids.add(self.tokenizer.eos_token_id)
                self.extra_stop_token_ids = list(stop_ids)
            except Exception:
                self.extra_stop_token_ids = []

        # AutoProcessor (مو الـ tokenizer وحده) لازم لبناء برومبت يحتوي صورة —
        # تحميله خفيف (إعدادات + معالج صور، بدون أوزان الموديل)، عكس تحميل
        # نسخة ثانية كاملة من الموديل.
        if PROCESSOR_AVAILABLE:
            try:
                self.processor = AutoProcessor.from_pretrained(
                    settings.model_name, token=settings.hf_token or None
                )
            except Exception:
                self.processor = None

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

    def render_multimodal_prompt(self, messages: List[Dict[str, object]]) -> str:
        """نفس فكرة render_prompt لكن عبر AutoProcessor بدل tokenizer وحده —
        لازم لصياغة برومبت يحتوي محتوى صورة (`{"type": "image"}`) بشكل صحيح.
        استخدمه فقط للطلبات اللي فيها multi_modal_data."""
        if self.processor is not None:
            return self.processor.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        return self.render_prompt(messages)

    async def generate_stream(
        self,
        prompt: str,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None,
        guided_json: Optional[dict] = None,
        result_holder: Optional[dict] = None,
        multi_modal_data: Optional[dict] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        """يبث الفروقات (delta) نصياً أولاً بأول — لإظهار بداية الرد بسرعة.

        `result_holder`: إن مُرِّر (dict فارغ)، يُعبَّأ بعد انتهاء التوليد بـ
        `stop_reason`/`finish_reason` — تُستخدم لمعرفة أي stop-string أوقف
        التوليد (مثل [ORDER_READY]) دون تسريب النص نفسه للعميل.

        `multi_modal_data`: مثلاً `{"image": pil_image}` — يستخدم نفس الموديل
        والأوزان المحمَّلة أصلاً (بدون نسخة ثانية). استخدم `render_multimodal_prompt`
        بدل `render_prompt` لبناء `prompt` عند تمرير هذا الوسيط.
        """
        sampling_kwargs = dict(
            max_tokens=max_tokens or settings.max_new_tokens,
            temperature=temperature if temperature is not None else settings.temperature,
            top_p=top_p if top_p is not None else settings.top_p,
            top_k=top_k if top_k is not None else settings.top_k,
        )
        if stop:
            sampling_kwargs["stop"] = stop
        if self.extra_stop_token_ids:
            sampling_kwargs["stop_token_ids"] = list(self.extra_stop_token_ids)
        if guided_json is not None:
            try:
                from vllm.sampling_params import GuidedDecodingParams

                sampling_kwargs["guided_decoding"] = GuidedDecodingParams(json=guided_json)
            except ImportError:
                pass  # نسخة أقدم من vLLM بدون guided decoding — نعتمد على وصف المخطط بالبرومبت فقط

        sampling_params = SamplingParams(**sampling_kwargs)
        request_id = str(uuid.uuid4())
        engine_prompt = (
            {"prompt": prompt, "multi_modal_data": multi_modal_data}
            if multi_modal_data else prompt
        )
        results_generator = self.engine.generate(
            engine_prompt, sampling_params, request_id, lora_request=self.lora_request
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
        multi_modal_data: Optional[dict] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
    ) -> str:
        chunks = []
        async for delta in self.generate_stream(
            prompt, max_tokens, temperature, stop, guided_json, result_holder,
            multi_modal_data, top_p, top_k,
        ):
            chunks.append(delta)
        return "".join(chunks)


llm_engine = LLMEngine()
