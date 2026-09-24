import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.llm import SYSTEM_PROMPT, LLMError
from app.features.district_correction.matching import candidate_names
from app.features.district_correction.normalization import normalize
from app.features.district_correction.models import CaseRequest, CorrectionRequest
from app.features.district_correction import router as service_router
from app.features.district_correction.router import _catalog_is_stale
from app.features.district_correction import auth as service_auth
from app import main as service_main


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp("catalog") / "districts.sqlite3"
    report = import_catalog(settings.district_source_dir, path)
    return Catalog(path), report, path


class NoLLM:
    configured = False


def case(sequence, district, address="", state="BGD", state_name=""):
    return CaseRequest(excelSequence=sequence, stateCode=state, stateName=state_name,
                       district=district, address=address)


def correct(catalog, cases, company="ALZAEEM", llm=None):
    service = CorrectionService(catalog, llm or NoLLM())
    request = CorrectionRequest(companyName=company, cases=cases)
    return asyncio.run(service.correct(request))[0].cases


def test_real_excel_structure_and_company_isolation(catalog):
    index, report, _ = catalog
    assert set(index.companies()) == {"ALZAEEM", "FUHOOD", "KHAYAL", "RIYAM", "TEST"}
    assert len(index.states) == 18
    assert report["companies"]["TEST"]["shared_governorates_used"]
    assert report["companies"]["FUHOOD"]["invalid_rows"] > 0
    assert report["companies"]["ALZAEEM"]["districts"] != report["companies"]["TEST"]["districts"]


def test_spelling_and_address_splitting(catalog):
    index = catalog[0]
    rows = correct(index, [
        case(1, "الكراده", "الكرادة شارع الرشيد، بناية 10"),
        case(3, "الكرادة شارع الرشيد، بناية 10"),
        case(5, "الكرادة", "الكرادة شارع الرشيد، بناية 10"),
    ])
    assert [(row.correctDistrict, row.status) for row in rows] == [
        ("الكرادة", "NORMALIZED_MATCH"), ("الكرادة", "SPLIT_ADDRESS"),
        ("الكرادة", "SPLIT_ADDRESS"),
    ]
    assert rows[0].addressDetails == "شارع الرشيد، بناية 10"
    assert rows[1].addressDetails == "شارع الرشيد، بناية 10"
    fuhood = correct(index, [case(2, "شطره", state="DHI"),
                             case(4, "الشطرة قرب السوق العام حي المعلمين", state="DHI")],
                     company="FUHOOD")
    assert [(row.correctDistrict, row.status) for row in fuhood] == [
        ("الشطرة", "NORMALIZED_MATCH"), ("الشطرة", "SPLIT_ADDRESS")]
    assert fuhood[1].addressDetails == "قرب السوق العام حي المعلمين"
    assert correct(index, [case(6, "شطره", state="DHI")])[0].correctDistrict != "الشطرة"


def test_unresolved_and_invalid_state_preserve_input(catalog):
    rows = correct(catalog[0], [case(1, "حي غير واضح", "قرب الجامع"),
                               case(2, "الكرادة", state="BAD")])
    assert rows[0].status == "UNRESOLVED"
    assert rows[0].correctDistrict == "حي غير واضح"
    assert rows[0].addressDetails == "قرب الجامع"
    assert rows[1].errorCode == "UNKNOWN_STATE"


def test_multiple_states_in_one_request(catalog):
    index = catalog[0]
    rows = correct(index, [case(1, "الكرادة", state="BGD"),
                           case(2, "الشطرة", state="DHI")], company="FUHOOD")
    assert [row.stateCode for row in rows] == ["BGD", "DHI"]
    assert all(row.status == "EXACT_MATCH" for row in rows)


def test_company_scope_is_hard_constraint(catalog):
    index = catalog[0]
    alzaeem = {item["name"] for item in index.districts("ALZAEEM", "BGD")}
    fuhood = {item["name"] for item in index.districts("FUHOOD", "BGD")}
    exclusive = next(name for name in alzaeem - fuhood if len(name) > 8)
    row = correct(index, [case(1, exclusive)], company="FUHOOD")[0]
    assert row.correctDistrict != exclusive or row.status == "UNRESOLVED"


def test_ambiguous_normalization_does_not_force_a_match():
    class AmbiguousCatalog:
        by_company = {"X": {"BGD": []}}
        states = {"BGD": {}}

        def districts(self, company, state_code):
            return [{"name": "الحارة"}, {"name": "الحاره"}]

    row = correct(AmbiguousCatalog(), [case(1, "حاره")], company="X")[0]
    assert row.status == "UNRESOLVED"
    assert row.correctDistrict == "حاره"


class FakeLLM:
    configured = True

    def __init__(self, answer):
        self.answer = answer

    async def resolve(self, company, state_code, cases, allowed_names):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_llm_cannot_invent_or_change_case(catalog):
    index = catalog[0]
    original = case(5, "مكان مجهول", "قرب الجامع")
    invented = [{"excelSequence": 5, "originalDistrict": original.district,
                 "stateCode": "BGD", "correctDistrict": "حي مخترع", "status": "AI_MATCH"}]
    row = correct(index, [original], llm=FakeLLM(invented))[0]
    assert row.status == "UNRESOLVED" and row.errorCode == "LLM_INVALID_RESPONSE"
    changed_sequence = [{"excelSequence": 6, "originalDistrict": original.district,
                         "stateCode": "BGD", "correctDistrict": "الكرادة", "status": "AI_MATCH"}]
    row = correct(index, [original], llm=FakeLLM(changed_sequence))[0]
    assert row.status == "UNRESOLVED" and row.correctDistrict == original.district


def test_llm_timeout_does_not_abort_batch(catalog):
    index = catalog[0]
    rows = correct(index, [case(1, "حي غير واضح"), case(2, "الكرادة")],
                   llm=FakeLLM(LLMError("LLM_TIMEOUT")))
    assert rows[0].errorCode == "LLM_TIMEOUT"
    assert rows[1].status == "EXACT_MATCH"


def test_large_batch_keeps_order(catalog):
    rows = correct(catalog[0], [case(number, "الكرادة") for number in range(1000)])
    assert len(rows) == 1000
    assert rows[999].excelSequence == 999
    assert all(row.status == "EXACT_MATCH" for row in rows)


def test_malformed_llm_json_is_reported_without_losing_case(monkeypatch):
    from app.features.district_correction import llm as llm_module

    class BadResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": "not json"}}]}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            return BadResponse()

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", Client)
    client = llm_module.LLMClient(replace(settings, district_llm_base_url="http://localhost:18001/v1", district_llm_model="test"))
    with pytest.raises(LLMError, match="LLM_INVALID_RESPONSE"):
        asyncio.run(client.resolve("FUHOOD", "DHI", [case(1, "شطره", state="DHI")], ["الشطرة"]))


def test_api_auth_errors_and_readiness(catalog, monkeypatch):
    index, _, path = catalog
    configured = replace(settings, district_database_path=path, district_api_key="secret")
    monkeypatch.setattr(service_router, "settings", configured)
    monkeypatch.setattr(service_auth, "settings", configured)
    with TestClient(service_main.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/district-correction/ready").json()["companies"] == [
            name for name in index.companies() if name != "TEST"]
        payload = {"companyName": "ALZAEEM", "cases": [{"excelSequence": 1, "stateCode": "BGD", "district": "الكرادة"}]}
        missing = client.post("/v1/district-correction", json=payload)
        assert missing.status_code == 422
        wrong = client.post("/v1/district-correction", headers={"X-API-Key": "wrong"}, json=payload)
        assert wrong.status_code == 401
        result = client.post("/v1/district-correction", headers={"X-API-Key": "secret"}, json=payload)
        assert result.status_code == 200 and result.json()["cases"][0]["status"] == "EXACT_MATCH"
        payload["companyName"] = "NO_COMPANY"
        assert client.post("/v1/district-correction", headers={"X-API-Key": "secret"}, json=payload).status_code == 404
        payload["companyName"] = "TEST"
        assert client.post("/v1/district-correction", headers={"X-API-Key": "secret"}, json=payload).status_code == 404
        non_ascii = client.post("/v1/district-correction", headers={"X-API-Key": "مفتاح".encode("utf-8")}, json=payload)
        assert non_ascii.status_code == 401
        payload["companyName"] = "ALZAEEM"
        payload["cases"].append(payload["cases"][0])
        invalid = client.post("/v1/district-correction", headers={"X-API-Key": "secret"}, json=payload)
        assert invalid.status_code == 422


def test_unconfigured_district_key_fails_closed(catalog, monkeypatch):
    _, _, path = catalog
    unconfigured = replace(settings, district_database_path=path, district_api_key="")
    monkeypatch.setattr(service_router, "settings", unconfigured)
    monkeypatch.setattr(service_auth, "settings", unconfigured)
    payload = {"companyName": "FUHOOD", "cases": [{"excelSequence": 1, "stateCode": "DHI", "district": "الشطرة"}]}
    with TestClient(service_main.app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/v1/district-correction/ready").status_code == 503
        response = client.post("/v1/district-correction", headers={"X-API-Key": "anything"}, json=payload)
        assert response.status_code == 500
        assert "غير مضبوط" in response.json()["detail"]


def test_postman_example_matches_integrated_endpoint(catalog, monkeypatch):
    _, _, path = catalog
    configured = replace(settings, district_database_path=path, district_api_key="secret")
    monkeypatch.setattr(service_router, "settings", configured)
    monkeypatch.setattr(service_auth, "settings", configured)
    collection_path = Path(__file__).resolve().parents[1] / "docs" / "district-correction-postman.json"
    collection = json.loads(collection_path.read_text(encoding="utf-8"))
    assert next(item["value"] for item in collection["variable"] if item["key"] == "district_api_key") == ""
    sample = collection["item"][2]["item"][0]["request"]
    assert sample["url"]["raw"] == "{{district_base_url}}/v1/district-correction"
    assert sample["header"][0]["value"] == "{{district_api_key}}"
    with TestClient(service_main.app) as client:
        response = client.post("/v1/district-correction", headers={"X-API-Key": "secret"},
                               json=json.loads(sample["body"]["raw"]))
    assert response.status_code == 200
    cases = response.json()["cases"]
    assert [item["correctDistrict"] for item in cases] == ["الشطرة", "الكرادة", "حي غير واضح"]
    assert cases[0]["addressDetails"] == "قرب السوق العام"


def test_prompt_requires_ai_match_status():
    assert "AI_MATCH" in SYSTEM_PROMPT


def test_valid_llm_choice_is_accepted(catalog):
    index = catalog[0]
    original = case(7, "مكان مجهول", "قرب الجامع")
    chosen = candidate_names(original.district, index.districts("ALZAEEM", "BGD"))[0]
    answer = [{"excelSequence": 7, "originalDistrict": original.district,
               "stateCode": "BGD", "correctDistrict": chosen, "status": "AI_MATCH"}]
    row = correct(index, [original], llm=FakeLLM(answer))[0]
    assert (row.status, row.correctDistrict, row.addressDetails) == ("AI_MATCH", chosen, "قرب الجامع")


def test_fuzzy_rejects_close_runner_up_hidden_by_prefilter(catalog):
    row = correct(catalog[0], [case(1, "الفلوجة - حي الضباط الاوله", state="ANB")])[0]
    assert row.status == "UNRESOLVED"


def test_llm_case_limit_marks_overflow(catalog):
    index = catalog[0]
    cases = [case(number, f"مكان مجهول {number}") for number in range(3)]
    service = CorrectionService(index, FakeLLM(LLMError("LLM_TIMEOUT")), max_llm_cases=1)
    request = CorrectionRequest(companyName="ALZAEEM", cases=cases)
    response, metrics = asyncio.run(service.correct(request))
    assert [row.errorCode for row in response.cases] == ["LLM_TIMEOUT", "LLM_LIMIT_EXCEEDED", "LLM_LIMIT_EXCEEDED"]
    assert metrics["sent_to_llm"] == 1


def test_persian_letters_and_eastern_digits_normalize():
    assert normalize("الحی") == normalize("الحي")
    assert normalize("کربلاء") == normalize("كربلاء")
    assert normalize("شارع ٤٠") == normalize("شارع ۴۰") == "شارع 40"


def test_state_code_is_trimmed_in_response(catalog):
    row = correct(catalog[0], [case(1, "الكرادة", state=" bgd ")])[0]
    assert (row.status, row.stateCode) == ("EXACT_MATCH", "BGD")


def test_oversized_fields_are_rejected():
    with pytest.raises(ValueError):
        case(1, "ا" * 301)
    with pytest.raises(ValueError):
        case(1, "الكرادة", "ا" * 1001)


def test_catalog_rebuilds_when_excel_is_newer(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    database = tmp_path / "catalog.sqlite3"
    assert _catalog_is_stale(source, database)
    database.write_bytes(b"")
    workbook = source / "cities.xlsx"
    workbook.write_bytes(b"")
    built_at = database.stat().st_mtime
    os.utime(workbook, (built_at + 10, built_at + 10))
    assert _catalog_is_stale(source, database)
    os.utime(workbook, (built_at - 10, built_at - 10))
    assert not _catalog_is_stale(source, database)
