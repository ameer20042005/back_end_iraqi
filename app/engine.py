# -*- coding: utf-8 -*-
"""غلاف حول transformers (AutoModelForImageTextToText) — يشغّل الموديل مباشرة
عبر مكتبة transformers الرسمية بدل vLLM، مع dynamic micro-batching يدوي
(queue + worker + توليد خطوة-بخطوة) لمحاكاة تزامن continuous batching.

**لماذا مو vLLM**: نسخة vLLM المتوفرة حالياً (0.25.1) عندها باغ معروف وغير
مُصلَح بعد بدعم معمارية Gemma4ForConditionalGeneration — يبني طبقة k_norm لكل
طبقات self-attention بشكل غير مشروط، بينما الموديل الحقيقي (KV-sharing بين
بعض الطبقات) لا يملك k_norm لكل الطبقات، فيفشل تحميل الأوزان
("weights were not initialized from checkpoint"). راجع
https://github.com/vllm-project/vllm/issues/44788 — لا يوجد إصلاح مدموج حتى
الآن رغم عدة محاولات متضاربة. transformers (المصدر الرسمي من Google/HF) لا
يعاني من هذا لأنه يحمّل المعمارية الحقيقية مباشرة — نفس ما اشتغل بنجاح في
gemma_iraqi_merge_fixed.ipynb.

**كيف يشتغل الـ batching هنا (بدون vLLM)**: بما إن `transformers.generate()`
الجاهزة لا تعطي بثاً منفصلاً لكل عنصر بدفعة batched (TextIteratorStreamer
مصمم لطلب واحد فقط)، البديل هو توليد يدوي خطوة-بخطوة: نبني دفعة بـ
`padding=True` (left padding)، ثم بكل خطوة نستدعي `model(input_ids, past_key_values=...)`
مرة وحدة للدفعة كاملة، نأخذ next-token لكل صف على حدة، نبثه فوراً لصاحبه عبر
Future/Queue خاص به، ونكمل حتى يتوقف كل الصفوف (كل صف يتوقف لحاله عند stop
token/string خاص فيه، والصفوف المتوقفة تُستبعد من الخطوات التالية عبر قناع
انتباه). هذا أبطأ وأعقد بكثير من continuous batching الحقيقي لـ vLLM
(PagedAttention يدير هذا على مستوى الذاكرة، هنا نديره يدوياً بكود Python) لكنه
يعطي تزامناً حقيقياً بدل قفل تسلسلي.

غير متوفر محلياً على Windows بدون GPU — عندها يبقى `ready = False` والباك اند
يرجع لوضع RAG-only (بدون توليد نموذج) حتى يشتغل الكود فعلياً على RunPod.
"""

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator, Dict, List, Optional

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


MAX_BATCH = 16       # أقصى عدد طلبات بدفعة واحدة
MAX_WAIT_MS = 30      # أقصى انتظار لتجميع دفعة قبل التنفيذ (أول طلب يشغّل المؤقّت)


@dataclass
class _PendingRequest:
    """طلب توليد واحد بانتظار دخول دفعة — كل طلب يبث نصه لقناة (asyncio.Queue)
    خاصة فيه، بغض النظر عن باقي الطلبات بنفس الدفعة."""

    input_ids: "torch.Tensor"          # (1, seq_len) — قبل الـ padding الجماعي
    max_new_tokens: int
    do_sample: bool
    temperature: float
    top_p: float
    top_k: int
    stop_strings: List[str]
    out_queue: "asyncio.Queue" = field(default_factory=asyncio.Queue)
    stop_reason: Optional[str] = None
    finished: bool = False


class LLMEngine:
    def __init__(self):
        self.model = None
        self.tokenizer = None
        self.processor = None  # للبرومبتات متعددة الوسائط (صورة/صوت) — انظر render_multimodal_prompt
        self.extra_stop_token_ids: List[int] = []  # مثلاً <end_of_turn> لـ Gemma — انظر start()
        self._queue: "asyncio.Queue" = asyncio.Queue()
        self._worker_task: Optional["asyncio.Task"] = None
        self._mm_lock = asyncio.Lock()  # طلبات الصور نادرة ولا تدخل الـ batching النصي العام
        self.metrics = {
            "requests_served": 0,
            "batches_run": 0,
            "batch_size_sum": 0,
            "latencies_ms": [],  # آخر 500 فقط — انظر _run_batch
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
            trust_remote_code=True,  # لازمة لمعمارية Gemma 4 متعددة الوسائط
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
        # إلزامي للتوليد بدفعة (batched generation) — بدونه المواضع النسبية
        # للتوكنات الحقيقية تختلف بين صفوف الدفعة وتكسر KV cache/attention mask.
        self.tokenizer.padding_side = "left"
        if self.tokenizer.pad_token_id is None:
            self.tokenizer.pad_token = self.tokenizer.eos_token

        # نمرر <end_of_turn> و eos_token_id الأساسي معاً كـ stop token ids —
        # الموديل يتوقف أحياناً بـ eos الأساسي بدل <end_of_turn>، والاعتماد على
        # واحد منهم فقط يخلي التوليد يكمل بعد نهاية الرد الفعلي وينتج نصاً
        # مشوّشاً (stop_ids = [106, 1] بالضبط بتوثيق النموذج على Hugging Face).
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

        self._loop = asyncio.get_event_loop()
        self._worker_task = asyncio.create_task(self._worker())

    async def shutdown(self) -> None:
        """يُستدعى من lifespan عند إيقاف السيرفر — يلغي الـ worker ويفشل أي
        طلبات لسا بالطابور بدل تركها معلَّقة إلى الأبد."""
        if self._worker_task is not None:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except (asyncio.CancelledError, Exception):
                pass
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            await req.out_queue.put(("error", RuntimeError("الخادم يتوقف")))

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
    # Micro-batching worker (نص فقط — طلبات الصور/الصوت تُعالَج بمعزل، انظر
    # generate_stream أدناه عند وجود multi_modal_data).
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        while True:
            try:
                first = await self._queue.get()
            except asyncio.CancelledError:
                return

            batch = [first]
            deadline = time.monotonic() + (MAX_WAIT_MS / 1000)
            while len(batch) < MAX_BATCH:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=remaining)
                    batch.append(item)
                except asyncio.TimeoutError:
                    break

            t0 = time.monotonic()
            try:
                await asyncio.to_thread(self._run_batch, batch)
            except Exception as exc:  # لا نترك أي طلب معلَّقاً لو فشلت الدفعة كاملة
                for req in batch:
                    await req.out_queue.put(("error", exc))
            finally:
                elapsed_ms = (time.monotonic() - t0) * 1000
                self.metrics["batches_run"] += 1
                self.metrics["batch_size_sum"] += len(batch)
                self.metrics["requests_served"] += len(batch)
                self.metrics["latencies_ms"].append(elapsed_ms)
                if len(self.metrics["latencies_ms"]) > 500:
                    self.metrics["latencies_ms"] = self.metrics["latencies_ms"][-500:]

    def get_metrics(self) -> dict:
        latencies = sorted(self.metrics["latencies_ms"])
        n = len(latencies)

        def _pct(p: float) -> Optional[float]:
            if not n:
                return None
            idx = min(n - 1, int(n * p))
            return round(latencies[idx], 1)

        avg_batch = (
            round(self.metrics["batch_size_sum"] / self.metrics["batches_run"], 2)
            if self.metrics["batches_run"] else 0
        )
        return {
            "requests_served": self.metrics["requests_served"],
            "batches_run": self.metrics["batches_run"],
            "avg_batch_size": avg_batch,
            "latency_ms_p50": _pct(0.50),
            "latency_ms_p95": _pct(0.95),
            "queue_depth": self._queue.qsize(),
        }

    def _run_batch(self, batch: List[_PendingRequest]) -> None:
        """تشغيل دفعة كاملة بتوليد يدوي خطوة-بخطوة (thread منفصل عبر
        asyncio.to_thread — استدعاءات GPU متزامنة تحجب الخيط لا event loop)."""
        tokenizer = self.tokenizer
        device = self.model.device
        n = len(batch)

        padded = tokenizer.pad(
            {"input_ids": [req.input_ids[0].tolist() for req in batch]},
            padding=True,
            return_tensors="pt",
        ).to(device)
        input_ids = padded["input_ids"]
        attention_mask = padded["attention_mask"]

        max_new = max(req.max_new_tokens for req in batch)
        finished = [False] * n
        generated_ids: List[List[int]] = [[] for _ in range(n)]
        sent_text: List[str] = ["" for _ in range(n)]  # ما أُرسل فعلياً لكل طلب — أساس حساب delta
        stop_ids_set = set(self.extra_stop_token_ids)

        past_key_values = None
        cur_input_ids = input_ids
        cur_attention_mask = attention_mask

        with torch.no_grad():
            for step in range(max_new):
                outputs = self.model(
                    input_ids=cur_input_ids,
                    attention_mask=cur_attention_mask,
                    past_key_values=past_key_values,
                    use_cache=True,
                )
                past_key_values = outputs.past_key_values
                next_token_logits = outputs.logits[:, -1, :]

                next_tokens = torch.empty(n, dtype=torch.long, device=device)
                for i, req in enumerate(batch):
                    if finished[i]:
                        next_tokens[i] = tokenizer.pad_token_id
                        continue
                    logits = next_token_logits[i]
                    if req.do_sample and req.temperature > 0:
                        logits = logits / req.temperature
                        if req.top_k:
                            top_v, top_i = torch.topk(logits, min(req.top_k, logits.size(-1)))
                            mask = torch.full_like(logits, float("-inf"))
                            mask[top_i] = top_v
                            logits = mask
                        probs = torch.softmax(logits, dim=-1)
                        next_id = torch.multinomial(probs, 1).item()
                    else:
                        next_id = int(torch.argmax(logits).item())
                    next_tokens[i] = next_id

                for i, req in enumerate(batch):
                    if finished[i]:
                        continue
                    token_id = int(next_tokens[i].item())
                    generated_ids[i].append(token_id)

                    is_eos = token_id in stop_ids_set
                    # نعيد فك تشفير كل التوكنات المولَّدة لهذا الصف حتى الآن
                    # (مو التوكن الأخير لحاله) — فك تشفير BPE/عربي لتوكن واحد
                    # منفصل قد يختلف عن فك التشفير التراكمي (دمج/تفكيك أحرف)،
                    # فنحسب delta كفرق نصي على decoded_so_far بدل الاعتماد على
                    # طول قائمة توكنات[:-1] (كان يعطي طولاً غير مطابق فعلياً
                    # لما أُرسل).
                    decoded_so_far = tokenizer.decode(generated_ids[i], skip_special_tokens=True)
                    hit_stop_string = next((s for s in req.stop_strings if s in decoded_so_far), None)

                    if hit_stop_string:
                        # لا نبث الجزء الذي يحمل stop string — نفس سلوك عدم
                        # تسريب [ORDER_READY]/[/TOOL_CALL] للعميل.
                        visible_full = decoded_so_far[: decoded_so_far.index(hit_stop_string)]
                        delta = visible_full[len(sent_text[i]):]
                        if delta:
                            sent_text[i] = visible_full
                            asyncio.run_coroutine_threadsafe(
                                req.out_queue.put(("delta", delta)), self._loop
                            )
                        req.stop_reason = hit_stop_string
                        finished[i] = True
                        asyncio.run_coroutine_threadsafe(
                            req.out_queue.put(("done", None)), self._loop
                        )
                    elif is_eos:
                        finished[i] = True
                        asyncio.run_coroutine_threadsafe(
                            req.out_queue.put(("done", None)), self._loop
                        )
                    else:
                        delta = decoded_so_far[len(sent_text[i]):]
                        if delta:
                            sent_text[i] = decoded_so_far
                            asyncio.run_coroutine_threadsafe(
                                req.out_queue.put(("delta", delta)), self._loop
                            )

                if all(finished):
                    break

                cur_input_ids = next_tokens.unsqueeze(-1)
                cur_attention_mask = torch.cat(
                    [cur_attention_mask, torch.ones((n, 1), dtype=cur_attention_mask.dtype, device=device)],
                    dim=-1,
                )

        for i, req in enumerate(batch):
            if not finished[i]:
                asyncio.run_coroutine_threadsafe(req.out_queue.put(("done", None)), self._loop)

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
        """يبث الفروقات (delta) نصياً أولاً بأول. الطلبات النصية تدخل طابور
        الـ micro-batching (`_worker`/`_run_batch`) وتُجمَّع مع طلبات أخرى
        متزامنة بدفعة واحدة (حتى `MAX_BATCH` أو `MAX_WAIT_MS`، أيهما أسبق).
        طلبات الصور (`multi_modal_data`) نادرة ولا تدعم padding بسهولة بهذا
        الشكل، فتُعالَج بمعزل عبر قفل بسيط بدل الدخول بالدفعة النصية.

        `result_holder`: إن مُرِّر (dict فارغ)، يُعبَّأ بعد انتهاء التوليد بـ
        `stop_reason`/`finish_reason` — تُستخدم لمعرفة أي stop-string أوقف
        التوليد (مثل [ORDER_READY]) دون تسريب النص نفسه للعميل.
        """
        if multi_modal_data and "image" in multi_modal_data:
            async for delta in self._generate_multimodal(
                prompt, max_tokens, temperature, stop, result_holder,
                multi_modal_data, top_p, top_k,
            ):
                yield delta
            return

        temp = temperature if temperature is not None else settings.temperature
        do_sample = temp is not None and temp > 0.0

        input_ids = self.tokenizer(prompt, return_tensors="pt")["input_ids"]

        req = _PendingRequest(
            input_ids=input_ids,
            max_new_tokens=max_tokens or settings.max_new_tokens,
            do_sample=do_sample,
            temperature=temp or 0.0,
            top_p=top_p if top_p is not None else settings.top_p,
            top_k=top_k if top_k is not None else settings.top_k,
            stop_strings=stop or [],
        )
        await self._queue.put(req)

        while True:
            kind, payload = await req.out_queue.get()
            if kind == "delta":
                yield payload
            elif kind == "error":
                raise payload
            elif kind == "done":
                break

        if result_holder is not None:
            result_holder["stop_reason"] = req.stop_reason
            result_holder["finish_reason"] = "stop" if req.stop_reason else "length"

    async def _generate_multimodal(
        self,
        prompt: str,
        max_tokens: Optional[int],
        temperature: Optional[float],
        stop: Optional[List[str]],
        result_holder: Optional[dict],
        multi_modal_data: dict,
        top_p: Optional[float],
        top_k: Optional[int],
    ) -> AsyncGenerator[str, None]:
        """مسار منفصل بدون batching لطلبات الصور — TextIteratorStreamer
        العادي (طلب واحد بنفس اللحظة عبر قفل) يكفي لأن هذي الطلبات نادرة
        (order_intake فقط) وplays لا تدخل على أداء الدردشة النصية العامة."""
        import threading

        from transformers import TextIteratorStreamer

        async with self._mm_lock:
            temp = temperature if temperature is not None else settings.temperature
            do_sample = temp is not None and temp > 0.0

            inputs = self.processor(
                text=prompt, images=multi_modal_data["image"], return_tensors="pt"
            ).to(self.model.device)

            streamer = TextIteratorStreamer(
                self.tokenizer, skip_prompt=True, skip_special_tokens=True
            )
            stop_ids = list(self.extra_stop_token_ids) or None

            gen_kwargs = dict(
                **inputs,
                max_new_tokens=max_tokens or settings.max_new_tokens,
                do_sample=do_sample,
                temperature=temp if do_sample else None,
                top_p=top_p if top_p is not None else settings.top_p,
                top_k=top_k if top_k is not None else settings.top_k,
                eos_token_id=stop_ids,
                streamer=streamer,
            )
            gen_kwargs = {k: v for k, v in gen_kwargs.items() if v is not None}

            thread = threading.Thread(target=self.model.generate, kwargs=gen_kwargs)
            thread.start()

            previous_text = ""
            full_text = ""
            loop = asyncio.get_event_loop()
            hit = None
            try:
                while True:
                    delta = await loop.run_in_executor(None, self._next_or_none, streamer)
                    if delta is None:
                        break
                    full_text += delta
                    if stop:
                        found = next((s for s in stop if s in full_text), None)
                        if found:
                            visible = full_text[: full_text.index(found)][len(previous_text):]
                            if visible:
                                yield visible
                            hit = found
                            break
                    yield delta
                    previous_text = full_text
            finally:
                thread.join(timeout=5)

            if result_holder is not None:
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


llm_engine = LLMEngine()
