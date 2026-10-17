# -*- coding: utf-8 -*-
"""بوابة إرسال نتيجة المكالمة لباك اند السستم — إرسال إضافي (best effort).

المصدر الأساسي للنتيجة هو مخزن الملفات (outcomes.py) اللي يسحبه تطبيق سطح
المكتب، ودائماً موجود. الإرسال الإضافي اختياري بالإعدادات:
  - باك اند السستم: `POST {system_backend_base_url}{SYSTEM_BACKEND_CALL_OUTCOME_PATH}`
    بمفتاح X-API-Key (المسار الافتراضي /calls/outcome؛ فارغ = معطّل).
  - باك اند البيانات Catalog: `POST {CATALOG_BASE_URL}{CATALOG_PATH_CALL_OUTCOME}`
    بتوكنه (المسار فارغ افتراضياً = معطّل).

تستدعيه أداة save_outcome (call_tools.store_outcome) بمساري LiveKit وWebSocket.
"""

from abc import ABC, abstractmethod

import httpx

from app.config import settings
from app.core import catalog
from app.core.system_backend import request as backend_request
from app.features.voice_followup.schema import CallOutcome


class CallOutcomeSubmitter(ABC):
    @abstractmethod
    async def submit(self, outcome: CallOutcome, api_key: str) -> bool:
        """يرسل نتيجة المكالمة. يرجع True لو أُرسلت، False لو الإرسال معطّل."""


class HttpCallOutcomeSubmitter(CallOutcomeSubmitter):
    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url
        self._timeout = timeout

    async def submit(self, outcome: CallOutcome, api_key: str) -> bool:
        base_url = self._base_url or settings.system_backend_base_url
        path = settings.system_backend_call_outcome_path
        if not base_url or not path:
            return False
        async with httpx.AsyncClient(base_url=base_url, timeout=self._timeout) as client:
            resp = await backend_request(
                client, "POST", path,
                json=outcome.model_dump(),
                headers={"X-API-Key": api_key},
            )
            resp.raise_for_status()
            return True


class CatalogCallOutcomeSubmitter(CallOutcomeSubmitter):
    async def submit(self, outcome: CallOutcome, api_key: str) -> bool:
        return await catalog.post_call_outcome(outcome.model_dump())


class CompositeCallOutcomeSubmitter(CallOutcomeSubmitter):
    """يجرّب كل الوجهات؛ فشل وحدة ما يمنع الباقي، ويُرفع بعد ما يخلصن."""

    def __init__(self, *submitters: CallOutcomeSubmitter) -> None:
        self._submitters = submitters

    async def submit(self, outcome: CallOutcome, api_key: str) -> bool:
        sent, error = False, None
        for submitter in self._submitters:
            try:
                sent = await submitter.submit(outcome, api_key) or sent
            except Exception as exc:
                error = exc
        if error is not None:
            raise error
        return sent


call_outcome_submitter: CallOutcomeSubmitter = CompositeCallOutcomeSubmitter(
    HttpCallOutcomeSubmitter(), CatalogCallOutcomeSubmitter(),
)
