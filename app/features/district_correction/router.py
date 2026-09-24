"""District correction endpoints within the existing FastAPI application."""

import json
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from .auth import require_district_api_key
from .catalog import Catalog, import_catalog
from app.config import settings
from .correction import CorrectionService
from .llm import LLMClient
from .models import CorrectionRequest, CorrectionResponse

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/district-correction", tags=["district-correction"])


def _catalog_is_stale(source_dir: Path, database_path: Path) -> bool:
    if not database_path.exists():
        return True
    built_at = database_path.stat().st_mtime
    return any(path.stat().st_mtime > built_at for path in Path(source_dir).rglob("*.xlsx"))


def load_catalog(app) -> None:
    """Load a snapshot during the main application's lifespan."""
    app.state.district_catalog = None
    app.state.district_catalog_error = None
    try:
        if _catalog_is_stale(settings.district_source_dir, settings.district_database_path):
            import_catalog(settings.district_source_dir, settings.district_database_path)
        app.state.district_catalog = Catalog(settings.district_database_path,
                                             settings.district_excluded_companies)
    except Exception as exc:
        app.state.district_catalog_error = type(exc).__name__
        logger.exception("district_catalog_startup_failed")


@router.get("/ready")
def ready(request: Request):
    catalog = getattr(request.app.state, "district_catalog", None)
    database_ok = False
    if settings.district_database_path.exists():
        try:
            with closing(sqlite3.connect(f"file:{settings.district_database_path.as_posix()}?mode=ro", uri=True)) as db:
                db.execute("SELECT 1 FROM companies LIMIT 1").fetchone()
                database_ok = True
        except sqlite3.Error:
            pass
    if not settings.district_api_key or catalog is None or not catalog.states or not catalog.companies() or not database_ok:
        raise HTTPException(status_code=503, detail={"code": "NOT_READY",
                                                     "startup_error": getattr(request.app.state, "district_catalog_error", None)})
    return {"status": "ready", "companies": catalog.companies(), "states": len(catalog.states)}


@router.post("", response_model=CorrectionResponse,
          dependencies=[Depends(require_district_api_key)])
async def correct(request_body: CorrectionRequest, request: Request):
    catalog = getattr(request.app.state, "district_catalog", None)
    if catalog is None:
        raise HTTPException(status_code=503, detail={"code": "CATALOG_UNAVAILABLE"})
    service = CorrectionService(catalog, LLMClient(settings), settings.district_llm_max_cases)
    started = time.monotonic()
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    try:
        response, metrics = await service.correct(request_body)
    except ValueError as exc:
        if str(exc) == "UNKNOWN_COMPANY":
            raise HTTPException(status_code=404, detail={"code": "UNKNOWN_COMPANY"}) from exc
        raise
    metrics.update({"event": "district_correction", "request_id": request_id,
                    "processing_ms": round((time.monotonic() - started) * 1000, 2)})
    logger.info(json.dumps(metrics, ensure_ascii=False))
    return response
