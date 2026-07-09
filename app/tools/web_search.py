# -*- coding: utf-8 -*-
"""أداة بحث ويب عام، متاحة لأي وكيل عبر app/tool_loop.py.

خلف واجهة عامة (WebSearchProvider) حتى يسهل استبدال المزوّد لاحقاً (Brave،
Serper...) بدون تغيير أي كود مستدعي. المزوّد الافتراضي **DuckDuckGo عبر مكتبة
`ddgs`** — بحث ويب حقيقي بدون أي API key أو تسجيل حساب. غير رسمي (بدون SLA
مضمون من DuckDuckGo)، فهو الخيار الأبسط للبدء وقابل للاستبدال بمزوّد رسمي لاحقاً
بنفس الطريقة.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import List


class WebSearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, max_results: int = 5) -> List[dict]:
        """يرجع نتائج بحث: [{"title", "url", "snippet"}, ...]"""


class DuckDuckGoWebSearchProvider(WebSearchProvider):
    """`ddgs.DDGS` متزامنة (sync) — ندوّرها بخيط منفصل عبر asyncio.to_thread
    حتى لا تحجب حلقة FastAPI غير المتزامنة."""

    def _search_sync(self, query: str, max_results: int) -> List[dict]:
        from ddgs import DDGS

        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [
            {"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")}
            for r in results
        ]

    async def search(self, query: str, max_results: int = 5) -> List[dict]:
        return await asyncio.to_thread(self._search_sync, query, max_results)


def get_web_search_provider() -> WebSearchProvider:
    return DuckDuckGoWebSearchProvider()


async def web_search_tool(args: dict) -> dict:
    """دالة أداة متوافقة مع app.tool_loop.ToolFunc — سجّلها بأي ميزة تحتاجها:
    tools={"web_search": web_search_tool}"""
    query = args.get("query")
    if not query:
        return {"error": "لازم تحدد query للبحث."}
    try:
        results = await get_web_search_provider().search(query, max_results=args.get("max_results", 5))
    except Exception as exc:
        return {"error": f"فشل البحث: {exc}"}
    return {"results": results}
