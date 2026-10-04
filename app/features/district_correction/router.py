"""District correction endpoints within the existing FastAPI application."""

import json
import logging
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from .aliases import AliasStore
from .auth import require_district_api_key
from .catalog import Catalog, import_catalog
from app.config import settings
from .correction import CorrectionService
from .llm import LLMClient
from .models import CorrectionRequest, CorrectionResponse, FeedbackRequest, FeedbackResponse
from .semantic import SemanticIndex

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
    app.state.district_aliases = None
    # Catalog vectors are cached in this object for the life of the process.
    app.state.district_semantic = SemanticIndex(settings)
    try:
        if _catalog_is_stale(settings.district_source_dir, settings.district_database_path):
            import_catalog(settings.district_source_dir, settings.district_database_path)
        app.state.district_catalog = Catalog(settings.district_database_path)
    except Exception as exc:
        app.state.district_catalog_error = type(exc).__name__
        logger.exception("district_catalog_startup_failed")
    try:
        app.state.district_aliases = AliasStore(settings.district_alias_database_path)
    except Exception:
        # Corrections still work without the memory; feedback then returns 503.
        logger.exception("district_alias_store_startup_failed")


def _service(request: Request) -> CorrectionService:
    catalog = getattr(request.app.state, "district_catalog", None)
    if catalog is None:
        raise HTTPException(status_code=503, detail={"code": "CATALOG_UNAVAILABLE"})
    return CorrectionService(catalog, LLMClient(settings), settings.district_llm_max_cases,
                             settings.district_llm_concurrency,
                             getattr(request.app.state, "district_aliases", None),
                             getattr(request.app.state, "district_semantic", None), settings.district_auto_learn)


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
    aliases = getattr(request.app.state, "district_aliases", None)
    semantic = getattr(request.app.state, "district_semantic", None)
    return {"status": "ready", "companies": catalog.companies(), "states": len(catalog.states),
            "rememberedCorrections": None if aliases is None else aliases.counts(),
            "autoLearn": settings.district_auto_learn,
            "semanticSearch": bool(semantic and semantic.configured)}


def _feedback(request_body: FeedbackRequest, request: Request, forget: bool) -> FeedbackResponse:
    service = _service(request)
    if service.aliases is None:
        raise HTTPException(status_code=503, detail={"code": "MEMORY_UNAVAILABLE"})
    try:
        response = service.remember(request_body, forget=forget)
    except ValueError as exc:
        if str(exc) == "UNKNOWN_COMPANY":
            raise HTTPException(status_code=404, detail={"code": "UNKNOWN_COMPANY"}) from exc
        raise
    logger.info(json.dumps({"event": "district_feedback", "company": response.companyName,
                            "forget": forget, "saved": response.saved, "removed": response.removed,
                            "rejected": len(response.rejected)}))
    return response


@router.get("/districts", dependencies=[Depends(require_district_api_key)])
def districts(companyName: str, stateCode: str, request: Request, stateName: str = ""):
    """Catalog names for one company and governorate (the review list on the test page)."""
    service = _service(request)
    company = companyName.strip().upper()
    if company not in service.catalog.by_company:
        raise HTTPException(status_code=404, detail={"code": "UNKNOWN_COMPANY"})
    code = service.resolve_state(stateCode, stateName)
    if code is None:
        raise HTTPException(status_code=404, detail={"code": "UNKNOWN_STATE"})
    return {"companyName": company, "stateCode": code,
            "districts": sorted(entry["name"] for entry in service.catalog.districts(company, code))}


@router.post("/feedback", response_model=FeedbackResponse, dependencies=[Depends(require_district_api_key)])
def remember(request_body: FeedbackRequest, request: Request):
    """Reviewer-confirmed districts; the same texts are then settled without the LLM."""
    return _feedback(request_body, request, forget=False)


@router.delete("/feedback", response_model=FeedbackResponse, dependencies=[Depends(require_district_api_key)])
def forget(request_body: FeedbackRequest, request: Request):
    """Remove remembered corrections (for example a confirmation made by mistake)."""
    return _feedback(request_body, request, forget=True)


@router.post("", response_model=CorrectionResponse,
          dependencies=[Depends(require_district_api_key)])
async def correct(request_body: CorrectionRequest, request: Request):
    service = _service(request)
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
