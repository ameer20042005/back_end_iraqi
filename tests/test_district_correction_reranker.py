"""The trained reranker: in shadow mode shown and logged beside the LLM, in decide mode the
stage between the rules and the LLM."""

import asyncio
import json
import time
from pathlib import Path

import pytest

from app.config import settings
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.models import CaseRequest, CorrectionRequest
from app.features.district_correction.reranker import ShadowLog, load_reranker

MODEL = Path(__file__).resolve().parents[1] / "training" / "models" / "reranker"


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp("reranker") / "districts.sqlite3"
    import_catalog(settings.district_source_dir, path)
    return Catalog(path)


class SlowLLM:
    configured = True

    def __init__(self, delay=0.0, pick=None):
        self.delay, self.pick, self.calls = delay, pick, []

    async def resolve(self, company, state_code, cases, allowed_names, hints=None, similar=None):
        self.calls += [case.district for case in cases]
        await asyncio.sleep(self.delay)
        return [{"excelSequence": case.excelSequence, "originalDistrict": case.district,
                 "correctDistrict": self.pick or hints.get(case.excelSequence), "addressDetails": "",
                 "stateCode": state_code, "status": "SPLIT_ADDRESS", "reason": "llm"} for case in cases]


class FakeReranker:
    name = "fake"

    def __init__(self, pick, delay=0.0, error=None):
        self.choice, self.delay, self.error, self.calls = pick, delay, error, []

    def pick(self, district, address, allowed, state_names=(), rules_pick=None):
        time.sleep(self.delay)  # runs in a worker thread, like the real model
        if self.error:
            raise self.error
        self.calls.append((district, rules_pick))
        return self.choice, 1.5


def correct(service, *districts, state="BGD"):
    cases = [CaseRequest(excelSequence=number, stateCode=state, district=district)
             for number, district in enumerate(districts, start=1)]
    return asyncio.run(service.correct(CorrectionRequest(companyName="FUHOOD", cases=cases)))


def test_shadow_pick_is_shown_and_logged_but_never_decides(catalog, tmp_path):
    log = ShadowLog(tmp_path / "shadow.jsonl")
    reranker = FakeReranker("ابو دشير")
    service = CorrectionService(catalog, SlowLLM(), reranker=reranker, shadow_log=log)
    response, metrics = correct(service, "بغداد الدورة ابو دشير شارع الزيتون", "بغداد العامرية")
    uncertain, certain = response.cases
    assert uncertain.correctDistrict == "الدورة" and uncertain.modelDistrict == "ابو دشير"
    assert certain.modelDistrict is None  # settled by the rules: nothing to shadow
    assert reranker.calls == [("بغداد الدورة ابو دشير شارع الزيتون", "الدورة")]
    assert (metrics["model_cases"], metrics["model_agreed"]) == (1, 0)
    record = json.loads((tmp_path / "shadow.jsonl").read_text(encoding="utf-8"))
    assert (record["rules"], record["final"], record["model"], record["reranker"]) == (
        "الدورة", "الدورة", "ابو دشير", "fake")


def test_reranker_runs_in_parallel_with_the_llm(catalog):
    service = CorrectionService(catalog, SlowLLM(delay=0.6), reranker=FakeReranker("الكرادة", delay=0.6))
    started = time.monotonic()
    response, metrics = correct(service, "بغداد الكرادة شارع الرشيد")
    assert time.monotonic() - started < 1.1  # 0.6 + 0.6 if they ran one after the other
    assert metrics["model_agreed"] == 1 and response.cases[0].modelDistrict == "الكرادة"


def test_unresolved_rows_are_shadowed_without_an_llm(catalog):
    class NoLLM:
        configured = False
    reranker = FakeReranker(None)
    response, metrics = correct(CorrectionService(catalog, NoLLM(), reranker=reranker), "حي غير واضح")
    assert response.cases[0].status == "UNRESOLVED" and metrics["model_cases"] == 1


def test_a_failing_reranker_never_breaks_the_request(catalog):
    service = CorrectionService(catalog, SlowLLM(), reranker=FakeReranker(None, error=RuntimeError("cuda")))
    response, metrics = correct(service, "بغداد الكرادة شارع الرشيد")
    assert response.cases[0].correctDistrict == "الكرادة" and metrics["model_cases"] == 0


def test_decide_mode_settles_agreement_without_the_llm(catalog):
    llm = SlowLLM()
    service = CorrectionService(catalog, llm, reranker=FakeReranker("الدورة"), reranker_decides=True)
    response, metrics = correct(service, "بغداد الدورة ابو دشير شارع الزيتون")
    case = response.cases[0]
    assert case.correctDistrict == "الدورة" and case.confidence >= 0.95 and "trained model agrees" in case.reason
    assert llm.calls == [] and (metrics["model_settled"], metrics["sent_to_llm"]) == (1, 0)


def test_decide_mode_sends_only_disagreement_to_the_llm(catalog):
    llm = SlowLLM(pick="ابو دشير")
    service = CorrectionService(catalog, llm, reranker=FakeReranker("ابو دشير"), reranker_decides=True)
    response, metrics = correct(service, "بغداد الدورة ابو دشير شارع الزيتون")
    assert llm.calls == ["بغداد الدورة ابو دشير شارع الزيتون"] and metrics["model_settled"] == 0
    assert response.cases[0].correctDistrict == "ابو دشير" and response.cases[0].modelDistrict == "ابو دشير"


def test_decide_mode_model_places_what_the_rules_could_not(catalog):
    llm = SlowLLM()
    service = CorrectionService(catalog, llm, reranker=FakeReranker("الكرادة"), reranker_decides=True)
    response, metrics = correct(service, "حي غير واضح")
    case = response.cases[0]
    assert (case.correctDistrict, case.confidence) == ("الكرادة", 0.9)
    assert case.addressDetails == "حي غير واضح"  # text the district is not written in stays as details
    assert llm.calls == [] and metrics["model_settled"] == 1


def test_decide_mode_keeps_unplaced_rows_when_the_model_is_unsure(catalog):
    llm = SlowLLM(pick="الكرادة")
    service = CorrectionService(catalog, llm, reranker=FakeReranker(None), reranker_decides=True)
    response, _ = correct(service, "حي غير واضح")
    assert response.cases[0].status == "UNRESOLVED" and llm.calls == []


def test_decide_mode_falls_back_to_the_llm_when_the_model_fails(catalog):
    llm = SlowLLM()
    service = CorrectionService(catalog, llm, reranker=FakeReranker(None, error=RuntimeError("cuda")),
                                reranker_decides=True)
    response, metrics = correct(service, "بغداد الكرادة شارع الرشيد")
    assert llm.calls == ["بغداد الكرادة شارع الرشيد"] and metrics["model_settled"] == 0
    assert response.cases[0].correctDistrict == "الكرادة"


def test_missing_model_or_libraries_disable_the_reranker(tmp_path):
    assert load_reranker("") is None
    assert load_reranker(tmp_path / "nothing-here") is None


@pytest.mark.skipif(not (MODEL / "district_reranker.json").exists(), reason="no trained model in training/models")
def test_trained_model_picks_a_catalog_name(catalog):
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    reranker = load_reranker(MODEL)
    name, score = reranker.pick("بغداد الدورة ابو دشير", "", catalog.districts("FUHOOD", "BGD"), ("بغداد", "BAGHDAD"))
    names = {entry["name"] for entry in catalog.districts("FUHOOD", "BGD")}
    assert (name is None or name in names) and isinstance(score, float)
