"""100 hard district cases for the AI: spelling mistakes and merged (glued) text.

The cases live in tests/data/district_hard_cases.json: real Excel names from five
company/governorate lists, each rewritten by a fixed rule (missing, doubled, swapped or
sound-alike letter; spaces removed; district glued to the address or to the governorate).
The expected answer is always the original Excel name.

The real model does the work (address separation, matching, normalization); all 100
cases go to it in one request per company, as Spring sends them. The tests run against
the model server used by the app (LM Studio on http://localhost:1234/v1 locally, or
DISTRICT_LLM_BASE_URL / LLM_BASE_URL) and are skipped only when no server answers.
"""

import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.llm import LLMClient
from app.features.district_correction.models import CaseRequest, CorrectionRequest

CASES = json.loads((Path(__file__).parent / "data" / "district_hard_cases.json").read_text(encoding="utf-8"))
IDS = [f"{index:03d}-{item['company']}-{item['state']}-{item['kind']}" for index, item in enumerate(CASES)]


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp("hard") / "districts.sqlite3"
    import_catalog(settings.district_source_dir, path)
    return Catalog(path)


def _model_settings():
    base = os.getenv("DISTRICT_LLM_BASE_URL") or os.getenv("LLM_BASE_URL") or "http://localhost:1234/v1"
    try:
        models = httpx.get(base.rstrip("/") + "/models", timeout=3).json()["data"]
        model = os.getenv("LLM_MODEL") or next(m["id"] for m in models if "embed" not in m["id"].lower())
    except Exception:
        return None
    return replace(settings, district_llm_base_url=base, district_llm_model=model,
                   district_llm_timeout_seconds=float(os.getenv("DISTRICT_LLM_TIMEOUT_SECONDS", "900")))


@pytest.fixture(scope="module")
def ai_rows(catalog):
    config = _model_settings()
    if config is None:
        pytest.skip("no model server answers; start LM Studio (start_local.sh) or vLLM")
    rows = {}
    for company in sorted({item["company"] for item in CASES}):
        numbered = [(index, item) for index, item in enumerate(CASES) if item["company"] == company]
        request = CorrectionRequest(companyName=company, cases=[
            CaseRequest(excelSequence=index, stateCode=item["state"], district=item["district"])
            for index, item in numbered])
        service = CorrectionService(catalog, LLMClient(config), max_llm_cases=len(numbered))
        for row in asyncio.run(service.correct(request))[0].cases:
            rows[row.excelSequence] = row
    return rows


def test_there_are_100_cases_from_real_excel_names(catalog):
    assert len(CASES) == 100
    for item in CASES:
        assert item["expected"] in {entry["name"] for entry in catalog.districts(item["company"], item["state"])}
        assert item["district"] != item["expected"]


@pytest.mark.parametrize("index", range(len(CASES)), ids=IDS)
def test_ai_returns_the_excel_district(ai_rows, index):
    item, row = CASES[index], ai_rows[index]
    assert row.errorCode is None, row.reason
    assert row.correctDistrict == item["expected"], row.reason
    assert row.status in ("AI_MATCH", "SPLIT_ADDRESS")
    assert row.originalDistrict == item["district"]
