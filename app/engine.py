# -*- coding: utf-8 -*-
"""غلاف حول transformers (AutoModelForImageTextToText.generate) — يشغّل الموديل
مباشرة عبر مكتبة transformers الرسمية بدل vLLM.

**لماذا مو vLLM**: نسخة vLLM المتوفرة حالياً (0.25.1) عندها باغ معروف وغير
مُصلَح بعد بدعم معمارية Gemma4ForConditionalGeneration — يبني طبقة k_norm لكل
طبقات self-attention بشكل غير مشروط، بينما الموديل الحقيقي (KV-sharing بين
بعض الطبقات) لا يملك k_norm لكل الطبقات، فيفشل تحميل الأوزان
("weights were not initialized from checkpoint"). راجع
https://github.com/vllm-project/vllm/issues/44788 — لا يوجد إصلاح مدموج حتى
الآن رغم عدة محاولات متضاربة. transformers (المصدر الرسمي من Google/HF) لا
يعاني من هذا لأنه يحمّل المعمارية الحقيقية مباشرة — نفس ما اشتغل بنجاح في
gemma_iraqi_merge_fixed.ipynb.

**لماذا generate() الجاهزة، مو حلقة توليد يدوية**: جُرِّب فعلياً محرك
micro-batching يدوي (استدعاء self.model() مباشرة بحلقة with past_key_values
متراكمة) لمحاكاة continuous batching، لكنه أنتج هذياناً كاملاً غير مترابط.
السبب: معمارية Gemma4 (KV-sharing بين طبقات attention محلية/عامة) تعتمد
داخلياً على `cache_position` دقيقة لتحديد أين تُكتب/تُقرأ القيم بالطبقات
المشتركة تحديداً — `generate()` الجاهزة تبنيها صحيحة تلقائياً عبر
prepare_inputs_for_generation، لكن استدعاء forward يدوياً بدونها (تمرير
position_ids فقط لا يكفي) يخلي الطبقات المشتركة تقرأ K/V بإزاحة خاطئة فينتج
هذياناً. الحل الآمن المُختبَر فعلياً (نفس ما نجح بالنوتبوك) هو استخدام
generate() نفسها.

**الثمن**: نفقد PagedAttention وContinuous Batching الحقيقي لـ vLLM — الطلبات
تُعالَج بالتتابع (قفل asyncio.Lock واحد حول كل استدعاء GPU) بدل تجميعها
ديناميكياً. مقبول لحجم حركة معتدل؛ يُعاد تقييمه لاحقاً إذا نضج دعم vLLM
لـ Gemma4 أو زاد الحمل بشكل يستدعي محرك batching حقيقي (بعد التحقق من دعمه
الفعلي لهذه المعمارية تحديداً، بما فيها آلية KV-sharing).

غير متوفر محلياً على Windows بدون GPU — عندها يبقى `ready = False` والباك اند
يرجع لوضع RAG-only (بدون توليد نموذج) حتى يشتغل الكود فعلياً على RunPod.
"""

import asyncio
import os
import threading
from typing import AsyncGenerator, Dict, List, Optional

from app.config import settings

from app.hf_utils import resolve_lora_path

try:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor, TextIteratorStreamer

    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False

try:
    from peft import PeftModel

    PEFT_AVAILABLE = True
except ImportError:
    PEFT_AVAILABLE = False


class LLMEngine:
    def __init__(self):
        self.model = None
        self.tokenizer = None
        self.processor = None  # للبرومبتات متعددة الوسائط (صورة/صوت) — انظر render_multimodal_prompt
        self.extra_stop_token_ids: List[int] = []  # مثلاً <end_of_turn> لـ Gemma — انظر start()
        self._lock = asyncio.Lock()  # طلب واحد بنفس اللحظة على GPU (بديل continuous batching)

    @property
    def ready(self) -> bool:
        return self.model is not None

    async def start(self) -> None:
        if not TRANSFORMERS_AVAILABLE:
            return

        # يسمح لـ huggingface_hub بالمصادقة تلقائياً عند تحميل موديل بوابة
        # (gated) مثل Gemma أو مستودع خاص.
        if settings.hf_token:
            os.environ.setdefault("HF_TOKEN", settings.hf_token)

        dtype = torch.bfloat16 if settings.dtype in ("auto", "bfloat16") else torch.float16

        model = AutoModelForImageTextToText.from_pretrained(
            settings.model_name,
            dtype=dtype,
            device_map="auto",
            trust_remote_code=True,   # لازمة لمعمارية Gemma 4 متعددة الوسائط
            attn_implementation="eager",  # مطلوب لمعمارية E4B (نفس وصفة النوتبوك المعتمدة)
        )

        if settings.lora_path and PEFT_AVAILABLE:
            lora_local_path = resolve_lora_path(settings.lora_path, settings.hf_token or None)
            model = PeftModel.from_pretrained(model, lora_local_path)
            model = model.merge_and_unload()

        model.eval()
        self.model = model

        self.processor = AutoProcessor.from_pretrained(
            settings.model_name, token=settings.hf_token or None
        )
        self.tokenizer = self.processor.tokenizer

        # اكتشاف توكنات التوقف **تلقائياً** من قالب المحادثة (نمط الـ probe
        # المعتمد بخلية الاستدلال الاحترافية في gemma_iraqi_merge_fixed.ipynb):
        # نبني محادثة قصيرة كاملة (user+assistant بدون generation prompt)
        # فتنتهي حتماً بتوكنات إغلاق الدور الحقيقية، ونلتقط منها التوكنات
        # الخاصة (special) الأخيرة. ممنوع كتابة اسم التوكن يدوياً
        # (convert_tokens_to_ids("<end_of_turn>")) — بهذا الموديل الاسم
        # المكتوب يدوياً يتحول بصمت لمعرّف خاطئ/UNK فالتوليد لا يتوقف أبداً
        # ويكمل يؤلف أدوار user/model وهمية بعد نهاية الرد (حصل فعلياً
        # بالاختبار على RunPod). المتوقع لهذا الموديل: [1, 106].
        try:
            probe = self.tokenizer.apply_chat_template(
                [{"role": "user", "content": "هلو"},
                 {"role": "assistant", "content": "هلا بيك"}],
                add_generation_prompt=False, return_dict=True, return_tensors="pt",
            )["input_ids"][0].tolist()
            special = set(self.tokenizer.all_special_ids)
            stop_ids = {t for t in probe[-3:] if t in special}
            if self.tokenizer.eos_token_id is not None:
                stop_ids.add(self.tokenizer.eos_token_id)
            self.extra_stop_token_ids = list(stop_ids)
        except Exception:
            self.extra_stop_token_ids = (
                [self.tokenizer.eos_token_id] if self.tokenizer.eos_token_id is not None else []
            )

    async def shutdown(self) -> None:
        """لا يوجد worker/طابور بالإصدار الحالي (قفل تسلسلي بسيط) — موجودة
        فقط لتماثل واجهة lifespan في app/main.py."""
        return

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

    def _build_stopping_criteria(self, prompt_len: int, stop: Optional[List[str]]):
        """StoppingCriteria نصي — transformers.generate() لا يدعم `stop`
        كسلاسل نصية أصلاً (فقط stop_token_ids)، فنفحص النص المُفكَّك أول
        بأول ونوقف التوليد يدوياً لحظة ظهور أي من السلاسل المطلوبة."""
        from transformers import StoppingCriteria, StoppingCriteriaList

        if not stop:
            return None

        tokenizer = self.tokenizer
        state = {"hit": None}

        class _StringStop(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs) -> bool:
                text = tokenizer.decode(input_ids[0][prompt_len:], skip_special_tokens=True)
                for s in stop:
                    if s in text:
                        state["hit"] = s
                        return True
                return False

        return StoppingCriteriaList([_StringStop()]), state

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
        """يبث الفروقات (delta) نصياً أولاً بأول عبر TextIteratorStreamer
        (يشتغل بخيط منفصل، نقرأ منه هنا بشكل async).

        `result_holder`: إن مُرِّر (dict فارغ)، يُعبَّأ بعد انتهاء التوليد بـ
        `stop_reason`/`finish_reason` — تُستخدم لمعرفة أي stop-string أوقف
        التوليد (مثل [ORDER_READY]) دون تسريب النص نفسه للعميل.

        `multi_modal_data`: مثلاً `{"image": pil_image}` — يستخدم نفس الموديل
        والأوزان المحمَّلة أصلاً (بدون نسخة ثانية). استخدم `render_multimodal_prompt`
        بدل `render_prompt` لبناء `prompt` عند تمرير هذا الوسيط.

        قفل واحد (`self._lock`) حول كل استدعاء GPU — بديل مبسّط لـ continuous
        batching الحقيقي: طلب واحد بنفس اللحظة، الباقي ينتظر بالدور.
        """
        async with self._lock:
            temp = temperature if temperature is not None else settings.temperature
            do_sample = temp is not None and temp > 0.0

            if multi_modal_data and "image" in multi_modal_data:
                inputs = self.processor(
                    text=prompt, images=multi_modal_data["image"], return_tensors="pt"
                ).to(self.model.device)
            else:
                inputs = self.tokenizer(prompt, return_tensors="pt").to(self.model.device)

            streamer = TextIteratorStreamer(
                self.tokenizer, skip_prompt=True, skip_special_tokens=True
            )

            stop_ids = list(self.extra_stop_token_ids) or None
            stopping = self._build_stopping_criteria(inputs["input_ids"].shape[1], stop)
            stopping_criteria, stop_state = stopping if stopping else (None, {"hit": None})

            # حتمي (do_sample=False) هو الوصفة المعتمدة الوحيدة — لا نمرر
            # temperature/top_p/top_k نهائياً عندها (تمريرها مع greedy يطلق
            # تحذيرات وقد يغيّر السلوك ببعض إصدارات transformers).
            gen_kwargs = dict(
                **inputs,
                max_new_tokens=max_tokens or settings.max_new_tokens,
                do_sample=do_sample,
                eos_token_id=stop_ids,
                stopping_criteria=stopping_criteria,
                streamer=streamer,
            )
            if do_sample:
                gen_kwargs["temperature"] = temp
                gen_kwargs["top_p"] = top_p if top_p is not None else settings.top_p
                gen_kwargs["top_k"] = top_k if top_k is not None else settings.top_k
            gen_kwargs = {k: v for k, v in gen_kwargs.items() if v is not None}

            thread = threading.Thread(target=self.model.generate, kwargs=gen_kwargs)
            thread.start()

            previous_text = ""
            full_text = ""
            loop = asyncio.get_event_loop()
            try:
                while True:
                    delta = await loop.run_in_executor(None, self._next_or_none, streamer)
                    if delta is None:
                        break
                    full_text += delta
                    # لا نبث النص بعد لحظة ظهور stop string — نفس سلوك vLLM
                    # (stop غير مُتضمَّن بالنص المُرسَل للعميل).
                    if stop:
                        hit = next((s for s in stop if s in full_text), None)
                        if hit:
                            visible = full_text[: full_text.index(hit)][len(previous_text):]
                            if visible:
                                yield visible
                            stop_state["hit"] = hit
                            break
                    yield delta
                    previous_text = full_text
            finally:
                thread.join(timeout=5)

            if result_holder is not None:
                hit = stop_state.get("hit")
                result_holder["stop_reason"] = hit
                result_holder["finish_reason"] = "stop" if hit else "length"

    @staticmethod
    def _next_or_none(streamer):
        try:
            return next(streamer)
        except StopIteration:
            return None

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

    def get_metrics(self) -> dict:
        """لا يوجد batching/طابور بهذا الإصدار (قفل تسلسلي بسيط) — إحصاءات
        بسيطة فقط لبقاء /metrics بـ app/main.py يعمل بلا كسر."""
        return {
            "mode": "sequential_lock",
            "note": "لا يوجد micro-batching حالياً — قفل asyncio.Lock واحد حول كل استدعاء GPU.",
        }


llm_engine = LLMEngine()
