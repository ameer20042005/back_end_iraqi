# -*- coding: utf-8 -*-
"""كتالوج المنتجات — الآن استعلام حقيقي على باك اند السستم، لا بيانات محلية.

`ProductRepository` واجهة مجرّدة بميثود `search()`/`get_by_id()` فقط.
البحث الدلالي (ترتيب النتائج، تطابق اللهجة...) مسؤولية باك اند السستم
بالكامل — هذا الباك اند (الذكاء) يمرر `query` نصياً حرفياً ويرجع ما يجيه،
بلا أي فهرسة أو منطق بحث محلي.

كل استدعاء يحمل مفتاح API الخاص بالخدمة (انظر app/auth.py) — باك اند السستم
هو من يحدد أي كتالوج منتجات يخص هذا المفتاح.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

import httpx

from app.config import settings
from app.system_backend import request as backend_request


class ProductRepository(ABC):
    @abstractmethod
    async def search(self, query: str, api_key: str, top_k: int = 5) -> List[dict]:
        """يرجع أفضل top_k منتج مطابق للاستعلام، حسب ترتيب باك اند السستم."""

    @abstractmethod
    async def get_by_id(self, product_id: str, api_key: str) -> Optional[dict]:
        """يرجع منتجاً واحداً بالمعرّف، أو None إذا غير موجود."""


class HttpProductRepository(ProductRepository):
    """عميل HTTP رفيع لباك اند السستم — بدون أي بيانات أو منطق بحث محلي.

    TODO: رابط ومسارات باك اند السستم الفعلية غير معروفة بعد —
    `SYSTEM_BACKEND_BASE_URL` (app/config.py) ومسارات `search`/`get_by_id`
    أدناه أفضل تخمين موثَّق. عدّلها فقط عند توفر التفاصيل الحقيقية؛ الواجهة
    (ProductRepository) والمستدعين (app/tools/products.py, sales/service.py)
    لا يتغيّرون.
    """

    def __init__(self, base_url: str = "", timeout: float = 15.0):
        self._base_url = base_url or settings.system_backend_base_url
        self._timeout = timeout

    def _headers(self, api_key: str) -> dict:
        return {"X-API-Key": api_key}

    async def search(self, query: str, api_key: str, top_k: int = 5) -> List[dict]:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await backend_request(
                client, "GET", "/products/search",
                params={"q": query, "top_k": top_k},
                headers=self._headers(api_key),
            )
            resp.raise_for_status()
            return resp.json().get("results", [])

    async def get_by_id(self, product_id: str, api_key: str) -> Optional[dict]:
        async with httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout) as client:
            resp = await backend_request(
                client, "GET", f"/products/{product_id}", headers=self._headers(api_key)
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()


product_repository: ProductRepository = HttpProductRepository()
