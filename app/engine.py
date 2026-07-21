# -*- coding: utf-8 -*-
"""عميل vLLM — الباك اند يتصل بخادم vLLM OpenAI-متوافق منفصل بدل تحميل
الموديل داخل عملية FastAPI.

**البنية** (حسب وصفة vLLM الرسمية لـ Gemma 4):
    ┌─────────────────────┐  HTTP   ┌──────────────────────────────┐
    │ FastAPI (منفذ 8000) │ ──────► │ vLLM serve (منفذ 8001)       │
    │ RAG + دروع + جلسات  │         │ gemma-iraqi-finetune-v2      │
    └─────────────────────┘         │ continuous batching حقيقي    │
                                    └──────────────────────────────┘

دعم Gemma4ForConditionalGeneration وصل لـ vLLM عبر PR #44429 (صورة
vllm/vllm-openai:gemma4-unified أو nightly wheel) — انظر Dockerfile/start.sh.
هذا يستبدل آلية transformers.generate() + micro-batching اليدوي السابقة
بالكامل: vLLM يدير PagedAttention وcontinuous batching داخلياً، فمئات
الطلبات المتزامنة تتقاسم الـ GPU تلقائياً بدون طوابير يدوية.

**قالب المحادثة يطبّقه vLLM بجهة الخادم** (/v1/chat/completions يستقبل
messages مباشرة) — لذلك render_prompt/render_multimodal_prompt صارتا تمريراً
مباشراً للرسائل، والراوترات ما تغيّرت.

**التوليد حتمي دائماً** (temperature=0.0) — وصفة النوتبوك المعتمدة الوحيدة؛
أي sampling أنتج انهيار مخرجات بالتجربة الفعلية. طلبات temperature تُتجاهل.

محلياً بدون خادم vLLM يبقى `ready = False` والباك اند يرجع لوضع fallback
(بدون توليد نموذج) — نفس السلوك السابق.
"""

import asyncio
import base64
import io
import logging
import time
from typing import AsyncGenerator, Dict, List, Optional, Union

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

Message = Dict[str, object]
# messages جاهزة، أو نص خام (توافقاً مع أي مستدعٍ قديم يمرر نصاً)
PromptLike = Union[str, List[Message]]

_READY_POLL_SECONDS = 10   # فترة إعادة فحص جاهزية خادم vLLM بالخلفية
_REQUEST_TIMEOUT = 120.0   # مهلة طلب توليد واحد (ثوانٍ)


def _image_to_data_uri(image) -> str:
    """يحوّل صورة PIL إلى data URI (base64 JPEG) بصيغة OpenAI image_url —
    خادم vLLM يستقبل الصور بهذه الصيغة عبر /v1/chat/completions."""
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=90)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


class LLMEngine:
    def __init__(self):
        self._ready = False
        self._client: Optional[httpx.AsyncClient] = None
        self._poller_task: Optional["asyncio.Task"] = None
        self.metrics = {
            "requests_served": 0,
            "request_latencies_ms": [],  # آخر 500 طلب
            "errors": 0,
        }

    @property
    def ready(self) -> bool:
        return self._ready

    # ------------------------------------------------------------------
    # دورة الحياة
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """يفتح عميل HTTP ويشغّل فاحص جاهزية بالخلفية — ما ننتظر vLLM هنا
        حتى لا نأخر إقلاع FastAPI (خادم vLLM يستغرق دقائق بتحميل الأوزان)؛
        الفاحص يقلب ready=True أول ما يجهز."""
        self._client = httpx.AsyncClient(
            base_url=settings.vllm_base_url, timeout=_REQUEST_TIMEOUT
        )
        self._poller_task = asyncio.create_task(self._readiness_poller())

    async def shutdown(self) -> None:
        if self._poller_task is not None:
            self._poller_task.cancel()
            try:
                await self._poller_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client is not None:
            await self._client.aclose()
        self._ready = False

    async def _probe(self) -> bool:
        """فحص واحد لجاهزية خادم vLLM (/v1/models يرجع 200 فقط بعد اكتمال
        تحميل الموديل فعلياً)."""
        try:
            resp = await self._client.get("/models", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def _readiness_poller(self) -> None:
        """يفحص الجاهزية دورياً: يلتقط إقلاع vLLM المتأخر عن FastAPI، ويكتشف
        سقوط الخادم لاحقاً فيرجّع الميزات لوضع fallback بدل أخطاء 500."""
        while True:
            was_ready = self._ready
            self._ready = await self._probe()
            if self._ready and not was_ready:
                logger.info("✅ خادم vLLM جاهز على %s", settings.vllm_base_url)
            elif was_ready and not self._ready:
                logger.warning("⚠️ خادم vLLM ما عاد يستجيب — الميزات رجعت لوضع fallback")
            await asyncio.sleep(_READY_POLL_SECONDS)

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

    async def _chat_completion(
        self,
        messages: List[Message],
        max_tokens: int,
        stop: Optional[List[str]],
        guided_json: Optional[dict],
    ) -> dict:
        body: dict = {
            "model": settings.model_name,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": 0.0,  # حتمي دائماً — sampling = انهيار مخرجات (مجرَّب)
        }
        if stop:
            body["stop"] = stop
        if guided_json:
            # structured outputs بصيغة OpenAI القياسية — vLLM يقيّد التوليد
            # بالمخطط فعلياً (guided decoding)، مو مجرد تلميح بالبرومبت.
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "extraction", "schema": guided_json},
            }

        t0 = time.monotonic()
        try:
            resp = await self._client.post("/chat/completions", json=body)
            resp.raise_for_status()
        except Exception:
            self.metrics["errors"] += 1
            raise
        finally:
            elapsed_ms = (time.monotonic() - t0) * 1000
            self.metrics["requests_served"] += 1
            self.metrics["request_latencies_ms"].append(elapsed_ms)
            if len(self.metrics["request_latencies_ms"]) > 500:
                self.metrics["request_latencies_ms"] = self.metrics["request_latencies_ms"][-500:]
        return resp.json()

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
            "vllm_base_url": settings.vllm_base_url,
            "vllm_ready": self._ready,
            "requests_served": self.metrics["requests_served"],
            "errors": self.metrics["errors"],
            "request_latency_ms_p50": _pct(0.50),
            "request_latency_ms_p95": _pct(0.95),
        }


llm_engine = LLMEngine()
