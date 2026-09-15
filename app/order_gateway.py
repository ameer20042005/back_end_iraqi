# -*- coding: utf-8 -*-
"""Submit confirmed orders to the external system without local storage."""

from abc import ABC, abstractmethod

from app.config import settings
from app.order_schema import OrderConfirmation
from app.system_backend import get_client, request as backend_request


class OrderSubmitter(ABC):
    @abstractmethod
    async def submit(self, order: OrderConfirmation, api_key: str) -> bool:
        """Send a confirmed order to the external system."""


class HttpOrderSubmitter(OrderSubmitter):
    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url or settings.system_backend_base_url
        self._timeout = timeout

    async def submit(self, order: OrderConfirmation, api_key: str) -> bool:
        await backend_request(
            get_client(),
            "POST",
            self._base_url.rstrip("/") + "/orders",
            json=order.model_dump(),
            headers={"X-API-Key": api_key},
            timeout=self._timeout,
        )
        return True


order_submitter: OrderSubmitter = HttpOrderSubmitter()
