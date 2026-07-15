# -*- coding: utf-8 -*-
"""غلاف حول transformers (AutoModelForImageTextToText.generate) — يشغّل الموديل
مباشرة عبر مكتبة transformers الرسمية بدل vLLM، مع dynamic micro-batching
مبني فوق generate() الجاهزة نفسها (مو حلقة توليد يدوية).

**لماذا مو vLLM**: نسخة vLLM المتوفرة حالياً (0.25.1) عندها باغ معروف وغير
مُصلَح بعد بدعم معمارية Gemma4ForConditionalGeneration — يبني طبقة k_norm لكل
طبقات self-attention بشكل غير مشروط، بينما الموديل الحقيقي (KV-sharing بين
بعض الطبقات) لا يملك k_norm لكل الطبقات، فيفشل تحميل الأوزان
("weights were not initialized from checkpoint"). راجع
https://github.com/vllm-project/vllm/issues/44788.

**لماذا batching فوق generate() وليس حلقة توليد يدوية**: جُرِّبت حلقة يدوية
(استدعاء self.model() مباشرة مع past_key_values متراكمة) وأنتجت هذياناً
كاملاً — معمارية Gemma4 (KV-sharing) تعتمد على `cache_position` دقيقة تبنيها
generate() داخلياً عبر prepare_inputs_for_generation، وتكرارها يدوياً هش وخطر.
generate() الجاهزة بالمقابل تدعم دفعة مبطَّنة (left padding) أصلاً وتدير الـ
cache صح لكل صفوف الدفعة — نفس الوصفة المجرَّبة بالنوتبوك لكن بدفعة بدل طلب
واحد. الثمن الوحيد: ماكو بث توكن-بتوكن لكل طلب على حدة (الرد يرجع كاملاً) —
وهذا مقبول لأن حارس الأرقام بالراوترات يجمّع الرد كاملاً قبل إرساله أصلاً.

**كيف يشتغل**: الطلبات النصية تدخل طابوراً؛ worker واحد يجمّعها (حتى MAX_BATCH
أو MAX_WAIT_MS، أيهما أسبق)، يرمّزها بدفعة واحدة بـ left padding، يستدعي
generate() مرة وحدة للدفعة كاملة بخيط منفصل (asyncio.to_thread)، يفك تشفير
كل صف على حدة، يقص عند stop strings الخاصة بكل طلب، ويحل Future كل طلب.
النتيجة: 10 طلبات متزامنة = دفعة واحدة بزمن قريب من زمن طلب واحد، بدل 10
أزمنة متراكمة بالتتابع (كانت توصل 110+ ثانية للطلب الأخير).

**التوليد حتمي دائماً** (do_sample=False) — وصفة النوتبوك المعتمدة الوحيدة؛
أي sampling أنتج انهيار مخرجات بالتجربة. طلبات temperature تُتجاهل عمداً.

طلبات الصور (multi_modal_data) نادرة ولا تُدمج بالدفعة النصية — مسار منفصل
بقفل بسيط.

غير متوفر محلياً على Windows بدون GPU — عندها يبقى `ready = False` والباك اند
يرجع لوضع RAG-only (بدون توليد نموذج) حتى يشتغل الكود فعلياً على RunPod.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, Dict, List, Optional, Tuple

from app.config import settings

from app.hf_utils import resolve_lora_path

try:
    import torch
    from transformers import AutoModelForImageTextToText, AutoProcessor

    TRANSFORMERS_AVAILABLE = True
except ImportError:
    TRANSFORMERS_AVAILABLE = False

try:
    from peft import PeftModel

    PEFT_AVAILABLE = True
except ImportError:
    PEFT_AVAILABLE = False


MAX_BATCH = 16    # أقصى عدد طلبات نصية بدفعة generate() واحدة
MAX_WAIT_MS = 30  # أقصى انتظار لتجميع الدفعة بعد وصول أول طلب


@dataclass
class _PendingRequest:
    prompt: str
    max_new_tokens: int
    stop_strings: List[str]
    future: "asyncio.Future" = field(default=None)


class LLMEngine:
    def __init__(self):
        self.model = None
        self.tokenizer = None
        self.processor = None  # للبرومبتات متعددة الوسائط (صورة/صوت) — انظر render_multimodal_prompt
        self.extra_stop_token_ids: List[int] = []  # تُكتشف تلقائياً بالـ probe — انظر start()
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self._worker_task: Optional["asyncio.Task"] = None
        self._mm_lock = asyncio.Lock()  # طلبات الصور فقط
        self.metrics = {
            "requests_served": 0,
            "batches_run": 0,
            "batch_size_sum": 0,
            "batch_latencies_ms": [],  # آخر 500 دفعة
        }

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
            attn_implementation="eager",  # مطلوب لمعمارية E4B (وصفة النوتبوك المعتمدة)
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
        # left padding إلزامي للتوليد بدفعة: مع right padding آخر توكن حقيقي
        # بالصفوف الأقصر يصير بمنتصف التسلسل وgenerate() تكمل من الـ padding.
        self.tokenizer.padding_side = "left"
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        # اكتشاف توكنات التوقف **تلقائياً** من قالب المحادثة (نمط الـ probe
        # المعتمد بالنوتبوك): نبني محادثة قصيرة كاملة فتنتهي حتماً بتوكنات
        # إغلاق الدور الحقيقية. ممنوع البحث بالاسم (convert_tokens_to_ids
        # ("<end_of_turn>")) — الاسم يتحول بصمت لمعرّف خاطئ بهذا الـ tokenizer
        # فالتوليد ما يتوقف ويؤلف أدوار user/model وهمية (حصل فعلياً).
        # المتوقع لهذا الموديل: [1, 106].
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

        self._worker_task = asyncio.create_task(self._worker())

    async def shutdown(self) -> None:
        """يُستدعى من lifespan عند إيقاف السيرفر — يلغي الـ worker ويفشل أي
        طلبات لسا بالطابور بدل تركها معلَّقة."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if req.future is not None and not req.future.done():
                req.future.set_exception(RuntimeError("الخادم يتوقف"))

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

    # ------------------------------------------------------------------
    # Micro-batching فوق generate() — نص فقط
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            deadline = time.monotonic() + (MAX_WAIT_MS / 1000)
            while len(batch) < MAX_BATCH:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self._queue.get(), timeout=remaining))
                except asyncio.TimeoutError:
                    break

            t0 = time.monotonic()
            try:
                results = await asyncio.to_thread(self._run_batch, batch)
            except Exception as exc:
                # لا نترك أي طلب معلَّقاً لو فشلت الدفعة — الجميع يستلم الخطأ
                # والـ worker يكمل للدفعة الجاية.
                for req in batch:
                    if req.future is not None and not req.future.done():
                        req.future.set_exception(exc)
            else:
                for req, res in zip(batch, results):
                    if req.future is not None and not req.future.done():
                        req.future.set_result(res)
            finally:
                elapsed_ms = (time.monotonic() - t0) * 1000
                self.metrics["batches_run"] += 1
                self.metrics["batch_size_sum"] += len(batch)
                self.metrics["requests_served"] += len(batch)
                self.metrics["batch_latencies_ms"].append(elapsed_ms)
                if len(self.metrics["batch_latencies_ms"]) > 500:
                    self.metrics["batch_latencies_ms"] = self.metrics["batch_latencies_ms"][-500:]

    def _run_batch(self, batch: List[_PendingRequest]) -> List[Tuple[str, Optional[str]]]:
        """يشغّل دفعة كاملة بـ generate() واحدة (يعمل بخيط منفصل عبر
        asyncio.to_thread). يرجع لكل طلب (النص بعد قص stop strings،
        وstop_reason إن وُجد)."""
        enc = self.tokenizer(
            [req.prompt for req in batch], return_tensors="pt", padding=True
        ).to(self.model.device)
        input_len = enc["input_ids"].shape[1]
        max_new = max(req.max_new_tokens for req in batch)

        with torch.no_grad():
            out = self.model.generate(
                **enc,
                max_new_tokens=max_new,
                do_sample=False,  # حتمي دائماً — وصفة النوتبوك؛ sampling = انهيار مخرجات
                eos_token_id=list(self.extra_stop_token_ids) or None,
                pad_token_id=self.tokenizer.pad_token_id,
            )

        results: List[Tuple[str, Optional[str]]] = []
        for i, req in enumerate(batch):
            text = self.tokenizer.decode(out[i][input_len:], skip_special_tokens=True)
            stop_reason = None
            for s in req.stop_strings:
                if s in text:
                    text = text[: text.index(s)]
                    stop_reason = s
                    break
            results.append((text, stop_reason))
        return results

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
        """الطلبات النصية تدخل طابور الـ micro-batching وتُنفَّذ بدفعة generate()
        مشتركة — الرد يُبث كقطعة واحدة كاملة بعد انتهاء الدفعة (ماكو بث
        توكن-بتوكن؛ حارس الأرقام بالراوترات يجمّع الرد كاملاً قبل إرساله أصلاً).
        طلبات الصور (multi_modal_data) تمر بمسار منفصل بقفل بسيط.

        `temperature`/`top_p`/`top_k` تُتجاهل عمداً — التوليد حتمي دائماً
        (do_sample=False)، أي sampling أنتج انهيار مخرجات بالتجربة الفعلية.

        `result_holder`: إن مُرِّر (dict فارغ)، يُعبَّأ بعد انتهاء التوليد بـ
        `stop_reason`/`finish_reason` — لمعرفة أي stop-string أوقف التوليد
        (مثل [ORDER_READY]) دون تسريب النص نفسه للعميل.
        """
        if multi_modal_data and "image" in multi_modal_data:
            async for delta in self._generate_multimodal(
                prompt, max_tokens, stop, result_holder, multi_modal_data
            ):
                yield delta
            return

        req = _PendingRequest(
            prompt=prompt,
            max_new_tokens=max_tokens or settings.max_new_tokens,
            stop_strings=stop or [],
            future=asyncio.get_running_loop().create_future(),
        )
        await self._queue.put(req)
        text, stop_reason = await req.future

        if result_holder is not None:
            result_holder["stop_reason"] = stop_reason
            result_holder["finish_reason"] = "stop" if stop_reason else "length"
        if text:
            yield text

    async def _generate_multimodal(
        self,
        prompt: str,
        max_tokens: Optional[int],
        stop: Optional[List[str]],
        result_holder: Optional[dict],
        multi_modal_data: dict,
    ) -> AsyncGenerator[str, None]:
        """مسار الصور — generate() لطلب واحد تحت قفل (نادر الاستخدام،
        order_intake فقط، فلا يبرر تعقيد دمجه بالدفعة النصية)."""
        async with self._mm_lock:
            inputs = self.processor(
                text=prompt, images=multi_modal_data["image"], return_tensors="pt"
            ).to(self.model.device)
            input_len = inputs["input_ids"].shape[1]

            def _gen():
                with torch.no_grad():
                    return self.model.generate(
                        **inputs,
                        max_new_tokens=max_tokens or settings.max_new_tokens,
                        do_sample=False,
                        eos_token_id=list(self.extra_stop_token_ids) or None,
                        pad_token_id=self.tokenizer.pad_token_id,
                    )

            out = await asyncio.to_thread(_gen)
            text = self.tokenizer.decode(out[0][input_len:], skip_special_tokens=True)

            stop_reason = None
            if stop:
                for s in stop:
                    if s in text:
                        text = text[: text.index(s)]
                        stop_reason = s
                        break

            if result_holder is not None:
                result_holder["stop_reason"] = stop_reason
                result_holder["finish_reason"] = "stop" if stop_reason else "length"
            if text:
                yield text

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
        latencies = sorted(self.metrics["batch_latencies_ms"])
        n = len(latencies)

        def _pct(p: float) -> Optional[float]:
            if not n:
                return None
            return round(latencies[min(n - 1, int(n * p))], 1)

        return {
            "mode": "generate_micro_batching",
            "requests_served": self.metrics["requests_served"],
            "batches_run": self.metrics["batches_run"],
            "avg_batch_size": (
                round(self.metrics["batch_size_sum"] / self.metrics["batches_run"], 2)
                if self.metrics["batches_run"] else 0
            ),
            "batch_latency_ms_p50": _pct(0.50),
            "batch_latency_ms_p95": _pct(0.95),
            "queue_depth": self._queue.qsize(),
        }


llm_engine = LLMEngine()
