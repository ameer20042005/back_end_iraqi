"""Regressions for bounded scheduling, glued-name retrieval and answer integrity."""

import asyncio
import json
from pathlib import Path

import pytest

from app.config import settings
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.llm import response_format
from app.features.district_correction.matching import candidate_names
from app.features.district_correction.models import CaseRequest, CorrectionRequest

HARD_CASES = json.loads((Path(__file__).parent / "data/district_hard_cases.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp("retrieval") / "districts.sqlite3"
    import_catalog(settings.district_source_dir, path)
    return Catalog(path)


@pytest.mark.parametrize("item", HARD_CASES, ids=[f"{i}-{c['kind']}" for i, c in enumerate(HARD_CASES)])
def test_hard_case_expected_name_is_in_top_30(catalog, item):
    allowed = catalog.districts(item["company"], item["state"])
    assert item["expected"] in candidate_names(item["district"], allowed, limit=30)


class SmallCatalog:
    states = {"BGD": {"name_ar": "بغداد"}, "BAS": {"name_ar": "البصرة"}}
    by_company = {"X": {"BGD": [{"name": "الكرادة"}], "BAS": [{"name": "العشار"}]}}

    def districts(self, company, code):
        return self.by_company[company][code]


def case(sequence, district="مكان مجهول", address="", code="BGD"):
    return CaseRequest(excelSequence=sequence, stateCode=code, district=district, address=address)


@pytest.mark.parametrize("concurrency", [1, 2, 3])
def test_batches_overlap_with_bound_and_keep_budget_order_and_scope(concurrency):
    async def run():
        class ControlledLLM:
            configured = True

            def __init__(self):
                self.active = 0
                self.peak = 0
                self.calls = []
                self.ready = asyncio.Event()
                self.release = asyncio.Event()

            async def resolve(self, company, code, cases, names, hints=None):
                self.active += 1
                self.peak = max(self.peak, self.active)
                self.calls.extend(c.excelSequence for c in cases)
                if self.active == concurrency:
                    self.ready.set()
                await self.release.wait()
                self.active -= 1
                name = "الكرادة" if code == "BGD" else "العشار"
                assert names == [name]
                return [{"excelSequence": c.excelSequence, "correctDistrict": name,
                         "stateCode": code, "status": "AI_MATCH"} for c in cases]

        llm = ControlledLLM()
        cases = [case(i, code="BGD" if i < 45 else "BAS") for i in range(80)]
        service = CorrectionService(SmallCatalog(), llm, max_llm_cases=65, llm_concurrency=concurrency)
        task = asyncio.create_task(service.correct(CorrectionRequest(companyName="X", cases=cases)))
        try:
            await asyncio.wait_for(llm.ready.wait(), timeout=3)
        finally:
            llm.release.set()
            response, metrics = await task
        assert llm.peak == concurrency
        assert sorted(llm.calls) == list(range(65))
        assert metrics["sent_to_llm"] == 65
        assert [r.excelSequence for r in response.cases] == list(range(80))
        assert all(r.status == "AI_MATCH" for r in response.cases[:65])
        assert all(r.errorCode == "LLM_LIMIT_EXCEEDED" for r in response.cases[65:])
        assert all(r.correctDistrict == "العشار" for r in response.cases[45:65])

    asyncio.run(run())


def test_one_failed_parallel_batch_does_not_lose_other_results():
    from app.features.district_correction.llm import LLMError

    class LLM:
        configured = True

        async def resolve(self, company, code, cases, names, hints=None):
            if cases[0].excelSequence == 0:
                raise LLMError("LLM_TIMEOUT")
            return [{"excelSequence": c.excelSequence, "correctDistrict": names[0],
                     "status": "AI_MATCH"} for c in cases]

    request = CorrectionRequest(companyName="X", cases=[case(i) for i in range(40)])
    response, _ = asyncio.run(CorrectionService(SmallCatalog(), LLM()).correct(request))
    assert all(row.errorCode == "LLM_TIMEOUT" for row in response.cases[:20])
    assert all(row.status == "AI_MATCH" for row in response.cases[20:])


@pytest.mark.parametrize("district", ["مكان مجهول", "الكراده"])
def test_duplicate_ai_answers_use_fallback_instead_of_first_answer(district):
    class LLM:
        configured = True

        async def resolve(self, company, code, cases, names, hints=None):
            answer = {"excelSequence": 1, "correctDistrict": "الكرادة", "status": "AI_MATCH"}
            return [answer, dict(answer, status="UNRESOLVED")]

    request = CorrectionRequest(companyName="X", cases=[case(1, district)])
    row = asyncio.run(CorrectionService(SmallCatalog(), LLM()).correct(request))[0].cases[0]
    if district == "مكان مجهول":
        assert row.errorCode == "LLM_INVALID_RESPONSE" and row.correctDistrict == district
    else:
        assert row.status == "NORMALIZED_MATCH" and row.errorCode is None
    assert "multiple answers" in row.reason


@pytest.mark.parametrize("district", ["مكان مجهول", "الكراده قرب الجامع"])
def test_empty_ai_details_do_not_erase_address(district):
    class LLM:
        configured = True

        async def resolve(self, company, code, cases, names, hints=None):
            return [{"excelSequence": 1, "correctDistrict": "الكرادة", "status": "SPLIT_ADDRESS",
                     "addressDetails": ""}]

    request = CorrectionRequest(companyName="X", cases=[case(1, district, "دار 15")])
    row = asyncio.run(CorrectionService(SmallCatalog(), LLM()).correct(request))[0].cases[0]
    assert "دار 15" in row.addressDetails
    if district.startswith("الكراده"):
        assert row.addressDetails == "قرب الجامع دار 15"


def test_glued_multiword_typo_stays_on_shortlist():
    allowed = [{"name": "مصفى بيجي"}, {"name": "حي الجامعة"}, {"name": "شارع فلسطين"}]
    assert candidate_names("مسفىبيجي قرب الجامع", allowed, limit=1) == ["مصفى بيجي"]
    assert candidate_names("مسفىبيجي", allowed, limit=0) == []


def test_schema_constrains_sequence_and_answer_count():
    schema = response_format(["الكرادة"], [case(7), case(12)])
    array = schema["json_schema"]["schema"]["properties"]["cases"]
    assert array["minItems"] == array["maxItems"] == 2
    assert array["items"]["properties"]["excelSequence"]["enum"] == [7, 12]
