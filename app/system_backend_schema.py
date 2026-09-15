# -*- coding: utf-8 -*-
"""Validated product contracts returned by the external system."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class SystemProduct(BaseModel):
    """One product returned by the external catalog API."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    price: float
    currency: Optional[str] = "IQD"
    in_stock: Optional[bool] = None
    stock_quantity: Optional[int] = None
    photos: List[str] = []
    deleted_at: Optional[str] = None


class SystemProductSearchResponse(BaseModel):
    """Response body for ``GET /products/search``."""

    model_config = ConfigDict(extra="allow")

    results: List[SystemProduct] = []
