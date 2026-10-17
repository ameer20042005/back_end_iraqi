# -*- coding: utf-8 -*-
"""بوابة إرسال نتيجة المكالمة لباك اند السستم — إرسال إضافي (best effort).

المصدر الأساسي للنتيجة هو مخزن الملفات (outcomes.py) اللي يسحبه تطبيق سطح
المكتب. لو `system_backend_base_url` مضبوط، تُرسل نفس النتيجة كمان إلى
`POST {system_backend_base_url}/calls/outcome` بمفتاح X-API-Key.

تستدعيه أداة save_outcome بالـ agent (agent.py).
"""

from abc import ABC, abstractmethod

import httpx

from app.config import settings
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
        if not base_url:
            return False
        async with httpx.AsyncClient(base_url=base_url, timeout=self._timeout) as client:
            resp = await backend_request(
                client, "POST", "/calls/outcome",
                json=outcome.model_dump(),
                headers={"X-API-Key": api_key},
            )
            resp.raise_for_status()
            return True


call_outcome_submitter: CallOutcomeSubmitter = HttpCallOutcomeSubmitter()
