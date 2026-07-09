# -*- coding: utf-8 -*-
"""
كتالوج المنتجات — مصدر بيانات وكيل المبيعات.

`ProductRepository` واجهة مجرّدة بميثود `search()`/`get_by_id()` فقط، حتى يسهل
استبدال `StaticProductRepository` (ملف JSON محلي) بمصدر حقيقي (قاعدة بيانات
خارجية أو API) بدون تغيير أي كود آخر في المشروع — أنشئ صنفاً جديداً يطبّق نفس
الواجهة، مثلاً:

    class PostgresProductRepository(ProductRepository):
        def search(self, query, top_k=5): ...
        def get_by_id(self, product_id): ...

    product_repository = PostgresProductRepository(dsn=...)

TODO: استبدل StaticProductRepository أدناه بقاعدة بيانات المنتجات الحقيقية
(PostgreSQL/MySQL/Supabase/MongoDB) عند توفر تفاصيل الاتصال.
"""

import json
import math
import os
from abc import ABC, abstractmethod
from collections import Counter, defaultdict
from typing import Dict, List, Optional

from app.rag.retriever import normalize, tokenize

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PRODUCTS_PATH = os.path.join(BASE_DIR, "data", "products.json")


class ProductRepository(ABC):
    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> List[dict]:
        """يرجع أفضل top_k منتج مطابق للاستعلام."""

    @abstractmethod
    def get_by_id(self, product_id: str) -> Optional[dict]:
        """يرجع منتجاً واحداً بالمعرّف، أو None إذا غير موجود."""


class StaticProductRepository(ProductRepository):
    """فهرس BM25 بالذاكرة فوق ملف JSON محلي (نفس أسلوب app/rag/retriever.py)."""

    def __init__(self, products_path: str = DEFAULT_PRODUCTS_PATH, k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.products: List[dict] = []
        self._by_id: Dict[str, dict] = {}
        self._doc_tokens: List[list] = []
        self._df = defaultdict(int)
        self._inverted = defaultdict(list)
        self._load(products_path)

    def _load(self, path: str) -> None:
        if not os.path.exists(path):
            raise FileNotFoundError(f"كتالوج المنتجات غير موجود: {path}")
        with open(path, encoding="utf-8") as f:
            self.products = json.load(f)

        for idx, product in enumerate(self.products):
            self._by_id[str(product["id"])] = product
            text = " ".join([
                product.get("name", ""),
                product.get("description", ""),
                product.get("category", ""),
                " ".join(product.get("tags", [])),
            ])
            tokens = tokenize(text, keep_stopwords=True)
            self._doc_tokens.append(tokens)
            for term, tf in Counter(tokens).items():
                self._df[term] += 1
                self._inverted[term].append((idx, tf))

        self._avgdl = (
            sum(len(t) for t in self._doc_tokens) / len(self._doc_tokens)
            if self._doc_tokens else 0.0
        )

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        q_tokens = tokenize(query)
        if not q_tokens:
            q_tokens = tokenize(query, keep_stopwords=True)
        if not q_tokens:
            return []

        n = len(self.products)
        scores: Dict[int, float] = defaultdict(float)
        for term in q_tokens:
            postings = self._inverted.get(term)
            if not postings:
                continue
            idf = math.log(1 + (n - self._df[term] + 0.5) / (self._df[term] + 0.5))
            for idx, tf in postings:
                dl = len(self._doc_tokens[idx])
                denom = tf + self.k1 * (1 - self.b + self.b * dl / self._avgdl)
                scores[idx] += idf * tf * (self.k1 + 1) / denom

        # مطابقة حرفية على اسم المنتج تتقدم على البقية
        q_norm = normalize(query)
        for idx, product in enumerate(self.products):
            if normalize(product.get("name", "")) in q_norm or q_norm in normalize(product.get("name", "")):
                scores[idx] += 5.0

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        return [{"score": round(s, 3), **self.products[i]} for i, s in ranked]

    def get_by_id(self, product_id: str) -> Optional[dict]:
        return self._by_id.get(str(product_id))


product_repository: ProductRepository = StaticProductRepository()
