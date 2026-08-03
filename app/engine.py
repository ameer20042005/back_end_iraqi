# -*- coding: utf-8 -*-
"""عميل vLLM — الباك اند يتصل بخوادم vLLM OpenAI-متوافقة منفصلة (نسخة أو
أكثر من نفس الموديل بالتوازي على نفس كارت GPU) بدل تحميل الموديل داخل عملية
FastAPI.

**البنية** (حسب وصفة vLLM الرسمية لـ Gemma 4، مع نسخ متوازية):
    ┌─────────────────────┐  HTTP   ┌──────────────────────────────┐
    │ FastAPI (منفذ 8000) │ ──────► │ vLLM serve #0 (منفذ 18001)   │
    │ RAG + دروع + جلسات  │  ─┐     │ gemma-iraqi-finetune-v2      │
    └─────────────────────┘   │     └──────────────────────────────┘
                               │     ┌──────────────────────────────┐
                               └───► │ vLLM serve #1 (منفذ 18002)   │
                                     │ نفس الموديل — نفس الكارت     │
                                     └──────────────────────────────┘

دعم Gemma4ForConditionalGeneration وصل لـ vLLM عبر PR #44429 (صورة
vllm/vllm-openai:gemma4-unified أو nightly wheel) — انظر Dockerfile/start.sh.
هذا يستبدل آلية transformers.generate() + micro-batching اليدوي السابقة
بالكامل: vLLM يدير PagedAttention وcontinuous batching داخلياً، فمئات
الطلبات المتزامنة تتقاسم الـ GPU تلقائياً بدون طوابير يدوية.

**نسخ متعددة بالتوازي** (start.sh يشغّلهن حسب VLLM_REPLICAS، انظر
app/config.py::resolved_vllm_base_urls): عملية vllm serve منفصلة لكل نسخة =
جدولة/طابور مستقلان، فطلب طويل بنسخة وحدة ما يؤخر طابور الثانية. هذا الملف
يوزّع كل طلب على النسخة الأقل ازدحاماً حالياً (least-outstanding-requests)
عبر _pick_replica، ويتتبّع جاهزية كل نسخة بشكل مستقل — سقوط نسخة وحدة يحوّل
كل الحمل تلقائياً للباقيات بدل رجوع كامل لوضع fallback.

**قالب المحادثة يطبّقه vLLM بجهة الخادم** (/v1/chat/completions يستقبل
messages مباشرة) — لذلك render_prompt/render_multimodal_prompt صارتا تمريراً
مباشراً للرسائل، والراوترات ما تغيّرت.

**التوليد حتمي دائماً** (temperature=0.0) — وصفة النوتبوك المعتمدة الوحيدة؛
أي sampling أنتج انهيار مخرجات بالتجربة الفعلية. طلبات temperature تُتجاهل.

محلياً بدون أي خادم vLLM جاهز يبقى `ready = False` والباك اند يرجع لوضع
fallback (بدون توليد نموذج) — نفس السلوك السابق.
"""

import asyncio
import base64
import io
import json
import logging
import time
from typing import AsyncGenerator, Dict, List, Optional, Union

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

Message = Dict[str, object]
# messages جاهزة، أو نص خام (توافقاً مع أي مستدعٍ قديم يمرر نصاً)
PromptLike = Union[str, List[Message]]

_READY_POLL_SECONDS = 10   # فترة إعادة فحص جاهزية خوادم vLLM بالخلفية
_REQUEST_TIMEOUT = 120.0   # مهلة طلب توليد واحد (ثوانٍ)


def _image_to_data_uri(image) -> str:
    """يحوّل صورة PIL إلى data URI (base64 JPEG) بصيغة OpenAI image_url —
    خادم vLLM يستقبل الصور بهذه الصيغة عبر /v1/chat/completions.

    الصورة واصلة هنا مصغَّرة أصلاً (انظر _downscale بـ vision.py)، فـ quality
    82 يقصّ حجم الـ base64 المنقول ~40% مقابل 90 بلا فرق مرئي على نص اللقطات.
    و`convert` يُطبَّق فقط إذا لزم — الصورة أصلاً RGB بمسارنا، والاستدعاء
    غير المشروط كان ينسخ الصورة كاملة بلا سبب."""
    buf = io.BytesIO()
    if image.mode != "RGB":
        image = image.convert("RGB")
    image.save(buf, format="JPEG", quality=82, optimize=False)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


class LLMEngine:
    def __init__(self):
        self._base_urls: List[str] = list(settings.resolved_vllm_base_urls)
        self._clients: List[httpx.AsyncClient] = []
        self._replica_ready: List[bool] = [False] * len(self._base_urls)
        self._inflight: List[int] = [0] * len(self._base_urls)
        self._poller_task: Optional["asyncio.Task"] = None
        self.metrics = {
            "requests_served": 0,
            "request_latencies_ms": [],  # آخر 500 طلب
            "errors": 0,
        }

    @property
    def ready(self) -> bool:
        """جاهز إذا نسخة واحدة على الأقل تستجيب — سقوط نسخة لا يوقف الميزات
        طالما نسخة ثانية بالتوازي شغّالة."""
        return any(self._replica_ready)

    # ------------------------------------------------------------------
    # دورة الحياة
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """يفتح عميل HTTP لكل نسخة vLLM ويشغّل فاحص جاهزية بالخلفية — ما
        ننتظر vLLM هنا حتى لا نأخر إقلاع FastAPI (خادم vLLM يستغرق دقائق
        بتحميل الأوزان)؛ الفاحص يقلب كل نسخة إلى ready=True أول ما تجهز."""
        self._clients = [
            httpx.AsyncClient(base_url=url, timeout=_REQUEST_TIMEOUT)
            for url in self._base_urls
        ]
        self._poller_task = asyncio.create_task(self._readiness_poller())

    async def shutdown(self) -> None:
        if self._poller_task is not None:
            self._poller_task.cancel()
            try:
                await self._poller_task
            except (asyncio.CancelledError, Exception):
                pass
        for client in self._clients:
            await client.aclose()
        self._replica_ready = [False] * len(self._base_urls)

    async def _probe(self, client: httpx.AsyncClient) -> bool:
        """فحص واحد لجاهزية نسخة vLLM (/v1/models يرجع 200 فقط بعد اكتمال
        تحميل الموديل فعلياً)."""
        try:
            resp = await client.get("/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def _readiness_poller(self) -> None:
        """يفحص جاهزية كل نسخة دورياً وباستقلالية: يلتقط إقلاع vLLM المتأخر
        عن FastAPI، ويكتشف سقوط أي نسخة لاحقاً فيوقف توجيه طلبات إلها (بلا
        ما يأثر على النسخ الباقية) — الرجوع لوضع fallback الكامل يصير فقط
        لو كل النسخ سقطت معاً."""
        while True:
            for i, client in enumerate(self._clients):
                was_ready = self._replica_ready[i]
                self._replica_ready[i] = await self._probe(client)
                if self._replica_ready[i] and not was_ready:
                    logger.info("✅ نسخة vLLM #%d جاهزة على %s", i, self._base_urls[i])
                elif was_ready and not self._replica_ready[i]:
                    logger.warning(
                        "⚠️ نسخة vLLM #%d (%s) ما عادت تستجيب", i, self._base_urls[i]
                    )
            await asyncio.sleep(_READY_POLL_SECONDS)

    def _pick_replica(self) -> int:
        """يختار النسخة الأقل ازدحاماً حالياً (least-outstanding-requests)
        من بين النسخ الجاهزة فقط — توزيع أحمال بسيط لكنه يتأقلم تلقائياً مع
        طلبات متفاوتة الطول (مقابل round-robin الأعمى)."""
        ready_indices = [i for i, r in enumerate(self._replica_ready) if r]
        if not ready_indices:
            raise RuntimeError("لا توجد نسخة vLLM جاهزة حالياً")
        return min(ready_indices, key=lambda i: self._inflight[i])

    # ------------------------------------------------------------------
    # صياغة البرومبت — تمرير مباشر (vLLM يطبّق قالب المحادثة بجهة الخادم)
    # ------------------------------------------------------------------

    def render_prompt(
        self, messages: List[Message], tools: Optional[List[dict]] = None
    ) -> List[Message]:
        """كان يحوّل الرسائل لنص عبر chat template محلي — الآن vLLM يطبّق
        القالب بجهة الخادم، فنمرر الرسائل كما هي. `tools` غير مستخدمة (بروتوكول
        الأدوات عندنا نصّي عبر app/tool_loop.py، مو native function-calling)."""
        return messages

    def render_multimodal_prompt(self, messages: List[Message]) -> List[Message]:
        """تمرير مباشر — تحويل محتوى الصورة لصيغة OpenAI يصير بـ _to_openai_messages
        وقت الإرسال (يحتاج الصورة نفسها من multi_modal_data)."""
        return messages

    # ------------------------------------------------------------------
    # التوليد عبر /v1/chat/completions
    # ------------------------------------------------------------------

    @staticmethod
    def _to_openai_messages(
        prompt: PromptLike, multi_modal_data: Optional[dict]
    ) -> List[Message]:
        """يطبّع المدخل لرسائل OpenAI: نص خام يُغلَّف كرسالة user، ومحتوى
        `{"type": "image"}` يُستبدل بـ image_url (data URI) من multi_modal_data."""
        if isinstance(prompt, str):
            messages: List[Message] = [{"role": "user", "content": prompt}]
        else:
            messages = [dict(m) for m in prompt]

        image = (multi_modal_data or {}).get("image")
        if image is None:
            return messages

        data_uri = _image_to_data_uri(image)
        for m in messages:
            content = m.get("content")
            if not isinstance(content, list):
                continue
            new_content = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image":
                    new_content.append(
                        {"type": "image_url", "image_url": {"url": data_uri}}
                    )
                else:
                    new_content.append(item)
            m["content"] = new_content
        return messages

    @staticmethod
    def _build_body(
        messages: List[Message],
        max_tokens: int,
        stop: Optional[List[str]],
        guided_json: Optional[dict],
        stream: bool = False,
    ) -> dict:
        body: dict = {
            "model": settings.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,  # حتمي دائماً — sampling = انهيار مخرجات (مجرَّب)
        }
        if stream:
            body["stream"] = True
        if stop:
            body["stop"] = stop
        if guided_json:
            # structured outputs بصيغة OpenAI القياسية — vLLM يقيّد التوليد
            # بالمخطط فعلياً (guided decoding)، مو مجرد تلميح بالبرومبت.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": guided_json},
            }
        return body

    async def _chat_completion(
        self,
        messages: List[Message],
        max_tokens: int,
        stop: Optional[List[str]],
        guided_json: Optional[dict],
    ) -> dict:
        body = self._build_body(messages, max_tokens, stop, guided_json)

        replica = self._pick_replica()
        self._inflight[replica] += 1
        t0 = time.monotonic()
        try:
            resp = await self._clients[replica].post("/chat/completions", json=body)
            resp.raise_for_status()
        except Exception:
            self.metrics["errors"] += 1
            raise
        finally:
            self._inflight[replica] -= 1
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.metrics["requests_served"] += 1
            self.metrics["request_latencies_ms"].append(elapsed_ms)
            if len(self.metrics["request_latencies_ms"]) > 500:
                self.metrics["request_latencies_ms"] = self.metrics["request_latencies_ms"][-500:]
        return resp.json()

    async def stream_chat_completion(
        self,
        messages: List[Message],
        max_tokens: int,
        stop: Optional[List[str]] = None,
    ) -> AsyncGenerator[str, None]:
        """توليد حر (بلا guided_json) مبثوث توكن-بتوكن فعلياً عبر SSE من vLLM
        (`stream: true`) — يُستخدم فقط للنص النهائي الحر بعد ما تنتهي جولات
        قرار الأدوات (انظر app/tool_loop.py::stream_final_answer). لا ينفع
        مع guided_json: JSON مقيَّد ما يصير صالحاً للتحليل إلا مكتملاً، فبثّه
        جزئياً بلا فائدة للعميل.

        كل قطعة SSE بصيغة OpenAI: `data: {...}\\n\\n`، تنتهي بـ `data: [DONE]`.
        نقص أي دلتا نص فيها ونتجاهل الباقي (role وما شابه)."""
        body = self._build_body(messages, max_tokens, stop, guided_json=None, stream=True)

        replica = self._pick_replica()
        self._inflight[replica] += 1
        t0 = time.monotonic()
        got_any = False
        try:
            async with self._clients[replica].stream("POST", "/chat/completions", json=body) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[len("data: "):]
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        got_any = True
                        yield delta
        except Exception:
            self.metrics["errors"] += 1
            raise
        finally:
            self._inflight[replica] -= 1
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.metrics["requests_served"] += 1
            self.metrics["request_latencies_ms"].append(elapsed_ms)
            if len(self.metrics["request_latencies_ms"]) > 500:
                self.metrics["request_latencies_ms"] = self.metrics["request_latencies_ms"][-500:]
        if not got_any:
            logger.error("⚠️ بث فارغ من vLLM (stream_chat_completion) — تحقق من المُرمِّز/الأوزان")

    async def generate_stream(
        self,
        prompt: PromptLike,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        stop: Optional[List[str]] = None,
        guided_json: Optional[dict] = None,
        result_holder: Optional[dict] = None,
        multi_modal_data: Optional[dict] = None,
        top_p: Optional[float] = None,
        top_k: Optional[int] = None,
    ) -> AsyncGenerator[str, None]:
        """يولّد الرد كاملاً ويبثّه كقطعة واحدة — عمداً مو توكن-بتوكن: حارس
        الأرقام/المواضيع بالراوترات يفحص الرد كاملاً قبل إرسال أي جزء للعميل،
        فالبث الجزئي ما ينفع أصلاً (رقم مختلَق مبثوث حياً ما ينسحب).

        `temperature`/`top_p`/`top_k` تُتجاهل عمداً — التوليد حتمي دائماً.

        `result_holder`: إن مُرِّر (dict فارغ)، يُعبَّأ بعد التوليد بـ
        `stop_reason` (أي stop-string أوقف التوليد، مثل [ORDER_READY]) و
        `finish_reason` — نفس عقد الواجهة السابق حرفياً.
        """
        messages = self._to_openai_messages(prompt, multi_modal_data)
        data = await self._chat_completion(
            messages,
            max_tokens=max_tokens or settings.max_new_tokens,
            stop=stop,
            guided_json=guided_json,
        )

        choice = data["choices"][0]
        text = (choice.get("message") or {}).get("content") or ""

        # vLLM يرجع stop_reason (حقل خاص به) = الـ stop string اللي أوقف
        # التوليد. احتياطاً (نسخ ما ترجعه): نقص النص يدوياً لو الـ stop وصل.
        stop_reason = choice.get("stop_reason")
        if isinstance(stop_reason, int):  # توكن إيقاف رقمي = إنهاء طبيعي
            stop_reason = None
        if stop and not stop_reason:
            for s in stop:
                if s in text:
                    text = text[: text.index(s)]
                    stop_reason = s
                    break

        if result_holder is not None:
            result_holder["stop_reason"] = stop_reason
            result_holder["finish_reason"] = (
                "stop" if (stop_reason or choice.get("finish_reason") == "stop") else "length"
            )

        # رد فارغ رغم توليد توكنات فعلي = عطل بمستوى المُرمِّز/الأوزان، مو
        # سلوك طبيعي. نصرخ باللوق بدل ما نمرره بصمت: وقع فعلاً (2026-07-26)
        # إن ترقية transformers لـ 5.x خلّت المُرمِّز يحمّل بـ vocab_size=5،
        # فصار كل نص عربي <unk> والموديل يرجّع فراغاً — وبقى العطل مخفي أيام
        # لأن اللوق كان يطبع finish_reason='stop' وكأن كل شي تمام.
        completion_tokens = (data.get("usage") or {}).get("completion_tokens") or 0
        if not text.strip() and completion_tokens > 0:
            logger.error(
                "⚠️ رد فارغ من vLLM رغم توليد %s توكن — تحقق من المُرمِّز "
                "(tokenizer.json موجود بالمستودع؟ vocab_size منطقي؟) أو من أوزان "
                "الموديل. finish_reason=%s stop_reason=%r",
                completion_tokens, choice.get("finish_reason"), stop_reason,
            )

        if text:
            yield text

    async def generate_full(
        self,
        prompt: PromptLike,
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
        latencies = sorted(self.metrics["request_latencies_ms"])
        n = len(latencies)

        def _pct(p: float) -> Optional[float]:
            if not n:
                return None
            return round(latencies[min(n - 1, int(n * p))], 1)

        return {
            "mode": "vllm_openai_client",
            "vllm_replicas": [
                {"base_url": url, "ready": ready, "inflight": inflight}
                for url, ready, inflight in zip(
                    self._base_urls, self._replica_ready, self._inflight
                )
            ],
            "vllm_ready": self.ready,
            "requests_served": self.metrics["requests_served"],
            "errors": self.metrics["errors"],
            "request_latency_ms_p50": _pct(0.50),
            "request_latency_ms_p95": _pct(0.95),
        }


llm_engine = LLMEngine()
