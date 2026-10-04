"""Optional semantic search over the catalog through an OpenAI-compatible embeddings server.

Catalog names are embedded once per company/state and kept in memory; customer texts
are embedded per request. The ranking is a second opinion beside the rules and a
better shortlist for the LLM, never an answer on its own.
"""

import asyncio

import httpx
import numpy as np

from app.config import Settings

_BATCH = 64


class EmbeddingError(Exception):
    pass


class SemanticIndex:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._vectors: dict[tuple, tuple[list[str], np.ndarray]] = {}
        self._locks: dict[tuple, asyncio.Lock] = {}

    @property
    def configured(self) -> bool:
        return bool(self.settings.district_embedding_base_url and self.settings.district_embedding_model)

    @property
    def agree_margin(self) -> float:
        return self.settings.district_embedding_agree_margin

    async def _embed(self, texts: list[str]) -> np.ndarray:
        """Unit-length vectors, one row per text."""
        url = self.settings.district_embedding_base_url.rstrip("/")
        if not url.endswith("/embeddings"):
            url += "/embeddings" if url.endswith("/v1") else "/v1/embeddings"
        key = self.settings.district_embedding_api_key
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        rows = []
        try:
            async with httpx.AsyncClient(timeout=self.settings.district_embedding_timeout_seconds) as client:
                for start in range(0, len(texts), _BATCH):
                    batch = texts[start:start + _BATCH]
                    response = await client.post(url, headers=headers, json={
                        "model": self.settings.district_embedding_model, "input": batch})
                    response.raise_for_status()
                    data = sorted(response.json()["data"], key=lambda item: item["index"])
                    if len(data) != len(batch):
                        raise ValueError("embedding count mismatch")
                    rows.extend(item["embedding"] for item in data)
        except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise EmbeddingError(type(exc).__name__) from exc
        matrix = np.asarray(rows, dtype=np.float32)
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        return matrix / np.where(norms == 0, 1, norms)

    async def _catalog(self, company: str, state_code: str, allowed: list[dict]):
        key = (self.settings.district_embedding_model, company, state_code)
        names = [entry["name"] for entry in allowed]
        cached = self._vectors.get(key)
        if cached is not None and cached[0] == names:
            return cached
        async with self._locks.setdefault(key, asyncio.Lock()):
            cached = self._vectors.get(key)
            if cached is None or cached[0] != names:
                prefix = self.settings.district_embedding_passage_prefix
                cached = (names, await self._embed([prefix + name for name in names]))
                self._vectors[key] = cached
        return cached

    async def rank(self, company: str, state_code: str, allowed: list[dict], texts: list[str],
                   limit: int = 10) -> list[list[tuple[str, float]]]:
        """Closest catalog names with cosine scores, best first, for each text."""
        if not texts or not allowed:
            return [[] for _ in texts]
        names, catalog = await self._catalog(company, state_code, allowed)
        unique = list(dict.fromkeys(texts))
        prefix = self.settings.district_embedding_query_prefix
        scores = await self._embed([prefix + text for text in unique]) @ catalog.T
        ranked = {}
        for text, row in zip(unique, scores):
            order = np.argsort(-row)[:limit]
            ranked[text] = [(names[i], float(row[i])) for i in order]
        return [ranked[text] for text in texts]
