# -*- coding: utf-8 -*-
"""حالة خادم النموذج: هل يستجيب؟ هل الموديل المطلوب محمَّل؟ هل يدعم الصور؟

لماذا لا يكفي GET /v1/models؟ LM Studio يعرض فيه **كل** الموديلات المنزَّلة
على القرص، محمَّلة أم لا — فوجود الاسم لا يعني أن الطلب سينجح. واجهته
الأصلية ``/api/v0/models`` تعطي الحقيقة: ``state`` (loaded/not-loaded) و
``type`` (vlm = موديل يقرأ الصور).

خادم vLLM لا يملك ``/api/v0`` ويعرض بـ ``/v1/models`` الموديلات المخدومة
فعلاً فقط، فوجود الاسم هناك يكفي للتحميل، ودعم الصور يُحسم من الإعداد.

النتيجة مخزَّنة ثوانيَ قليلة: كل طلب محادثة يفحص الحالة قبل التوليد، ولا
داعي لنداءين إضافيين بكل رسالة، لكن تفريغ الموديل يجب أن يظهر بسرعة.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 3.0
_PROBE_TIMEOUT = 5.0


@dataclass(frozen=True)
class ModelStatus:
    model: str
    reachable: bool
    # None = تعذّر الحسم (الخادم لا يوفّر المعلومة).
    loaded: Optional[bool]
    vision: Optional[bool]
    backend: str  # "lmstudio" أو "openai-compatible" أو "unreachable"

    @property
    def supports_vision(self) -> bool:
        """القرار النهائي: الإعداد الصريح يتقدّم، ثم ما أعلنه الخادم."""
        if settings.llm_vision in ("true", "1", "yes"):
            return True
        if settings.llm_vision in ("false", "0", "no"):
            return False
        return True if self.vision is None else self.vision

    def as_dict(self) -> dict:
        return {
            "model": self.model,
            "reachable": self.reachable,
            "loaded": self.loaded,
            "vision": self.supports_vision,
            "vision_reported_by_server": self.vision,
            "backend": self.backend,
            "base_url": settings.vllm_base_url,
        }


_cache: Optional[ModelStatus] = None
_cache_at = 0.0


def _server_root(base_url: str) -> str:
    """http://host:1234/v1 → http://host:1234 (جذر واجهة LM Studio الأصلية)."""
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


async def _probe(model: str, transport: Optional[httpx.AsyncBaseTransport] = None) -> ModelStatus:
    base = settings.vllm_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT, transport=transport) as client:
        try:
            listed = await client.get(f"{base}/models")
        except httpx.HTTPError as exc:
            logger.warning("Model server unreachable at %s: %s", base, exc)
            return ModelStatus(model, False, None, None, "unreachable")

        try:
            native = await client.get(f"{_server_root(base)}/api/v0/models")
            native_data = native.json().get("data") if native.status_code == 200 else None
        except (httpx.HTTPError, ValueError):
            native_data = None

    if isinstance(native_data, list) and any("state" in m for m in native_data if isinstance(m, dict)):
        entry = next((m for m in native_data if isinstance(m, dict) and m.get("id") == model), None)
        if entry is None:
            return ModelStatus(model, True, False, None, "lmstudio")
        return ModelStatus(
            model, True, entry.get("state") == "loaded", entry.get("type") == "vlm", "lmstudio",
        )

    try:
        ids = [m.get("id") for m in listed.json().get("data", []) if isinstance(m, dict)]
    except ValueError:
        ids = []
    loaded = (model in ids) if listed.status_code == 200 and ids else None
    return ModelStatus(model, listed.status_code < 500, loaded, None, "openai-compatible")


async def get_model_status(force: bool = False) -> ModelStatus:
    global _cache, _cache_at
    now = time.monotonic()
    if not force and _cache is not None and now - _cache_at < _CACHE_SECONDS:
        return _cache
    _cache = await _probe(settings.model_name)
    _cache_at = now
    return _cache
