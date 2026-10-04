"""Reviewer-confirmed correction memory and the optional semantic search layer."""

import asyncio
import json
import sqlite3
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main as service_main
from app.config import settings
from app.features.district_correction import auth as service_auth
from app.features.district_correction import router as service_router
from app.features.district_correction import semantic as semantic_module
from app.features.district_correction.aliases import AliasStore
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.models import CaseRequest, CorrectionRequest, FeedbackRequest
from app.features.district_correction.semantic import EmbeddingError, SemanticIndex


@pytest.fixture(scope="module")
def catalog_path(tmp_path_factory):
    path = tmp_path_factory.mktemp("memory") / "districts.sqlite3"
    import_catalog(settings.district_source_dir, path)
    return path


@pytest.fixture(scope="module")
def catalog(catalog_path):
    return Catalog(catalog_path)


class FakeLLM:
    configured = True

    def __init__(self, pick=None):
        self.pick = pick
        self.calls = []
        self.similar = None

    async def resolve(self, company, state_code, cases, allowed_names, hints=None, similar=None):
        self.calls.append([case.excelSequence for case in cases])
        self.similar = similar
        return [{"excelSequence": case.excelSequence, "originalDistrict": case.district,
                 "correctDistrict": self.pick or hints.get(case.excelSequence), "addressDetails": "",
                 "stateCode": state_code, "status": "AI_MATCH", "reason": "llm"} for case in cases]


class FakeSemantic:
    configured = True
    agree_margin = 0.03

    def __init__(self, ranking):
        self.ranking = ranking
        self.texts = []

    async def rank(self, company, state_code, allowed, texts, limit=10):
        self.texts.extend(texts)
        if isinstance(self.ranking, Exception):
            raise self.ranking
        return [self.ranking for _ in texts]


def feedback(district, correct, state="BGD", company="FUHOOD"):
    return FeedbackRequest(companyName=company, corrections=[
        {"stateCode": state, "district": district, "correctDistrict": correct}])


def correct(service, district, state="BGD", company="FUHOOD"):
    request = CorrectionRequest(companyName=company, cases=[
        CaseRequest(excelSequence=1, stateCode=state, district=district)])
    return asyncio.run(service.correct(request))


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def test_alias_store_persists_counts_confirmations_and_forgets(tmp_path):
    store = AliasStore(tmp_path / "aliases.sqlite3")
    store.learn([("FUHOOD", "BGD", "دوره ابو دشير", "ابو دشير", "الدورة ابو دشير")])
    store.learn([("FUHOOD", "BGD", "دوره ابو دشير", "ابو دشير", "الدوره ابو دشير")])
    reopened = AliasStore(tmp_path / "aliases.sqlite3")
    assert reopened.get("FUHOOD", "BGD", "دوره ابو دشير") == ("ابو دشير", "reviewer") and len(reopened) == 1
    with sqlite3.connect(tmp_path / "aliases.sqlite3") as db:
        assert db.execute("SELECT confirmations FROM district_aliases").fetchone()[0] == 2
    assert reopened.forget([("FUHOOD", "BGD", "دوره ابو دشير")]) == 1
    assert AliasStore(tmp_path / "aliases.sqlite3").get("FUHOOD", "BGD", "دوره ابو دشير") is None


def test_confirmed_text_is_settled_without_the_llm(catalog, tmp_path):
    llm = FakeLLM(pick="دورة أبو طيارة")
    service = CorrectionService(catalog, llm, aliases=AliasStore(tmp_path / "a.sqlite3"))
    saved = service.remember(feedback("بغداد الدورة ابو دشير شارع الزيتون", "ابو دشير"))
    assert (saved.saved, saved.rejected) == (1, [])
    # Same text up to spelling, punctuation and the governorate name.
    response, metrics = correct(service, "بغداد / الدوره ابو دشير شارع الزيتون")
    row = response.cases[0]
    assert (row.correctDistrict, row.status, row.confidence) == ("ابو دشير", "SPLIT_ADDRESS", 0.98)
    assert row.addressDetails == "الدوره شارع الزيتون"
    assert llm.calls == [] and metrics["remembered"] == 1


def test_learned_spelling_keeps_the_typed_place_in_details(catalog, tmp_path):
    service = CorrectionService(catalog, FakeLLM(), aliases=AliasStore(tmp_path / "a.sqlite3"))
    service.remember(feedback("اربيل حاكماوه", "حاجياوا", state="ARB"))
    row = correct(service, "اربيل  حاكماوه", state="ARB")[0].cases[0]
    assert (row.correctDistrict, row.addressDetails) == ("حاجياوا", "حاكماوه")


def test_memory_is_scoped_to_company_and_state(catalog, tmp_path):
    llm = FakeLLM()
    service = CorrectionService(catalog, llm, aliases=AliasStore(tmp_path / "a.sqlite3"))
    service.remember(feedback("بغداد الدورة ابو دشير", "ابو دشير"))
    row = correct(service, "بغداد الدورة ابو دشير", company="ALZAEEM")[0].cases[0]
    assert "remembered" not in row.reason


def test_feedback_rejects_unknown_names_states_and_empty_text(catalog, tmp_path):
    service = CorrectionService(catalog, FakeLLM(), aliases=AliasStore(tmp_path / "a.sqlite3"))
    request = FeedbackRequest(companyName="FUHOOD", corrections=[
        {"stateCode": "BGD", "district": "بغداد الدورة", "correctDistrict": "ليس في الكتالوج"},
        {"stateCode": "XXX", "district": "بغداد الدورة", "correctDistrict": "الدورة"},
        {"stateCode": "BGD", "district": "بغداد", "correctDistrict": "الدورة"},
        {"stateCode": "BGD", "district": "بغداد الدورة"},
        {"stateCode": "KOT", "district": "الكوت داموك", "correctDistrict": "داموك"},
    ])
    response = service.remember(request)
    assert [(item.index, item.code) for item in response.rejected] == [
        (0, "UNKNOWN_DISTRICT"), (1, "UNKNOWN_STATE"), (2, "EMPTY_TEXT"), (3, "MISSING_DISTRICT")]
    assert response.saved == 1  # the legacy code KOT is stored under WST
    with pytest.raises(ValueError, match="UNKNOWN_COMPANY"):
        service.remember(feedback("بغداد الدورة", "الدورة", company="NOPE"))


def test_remembered_name_missing_from_a_new_catalog_is_ignored(catalog, tmp_path):
    store = AliasStore(tmp_path / "a.sqlite3")
    store.learn([("FUHOOD", "BGD", "دوره", "اسم حذف من الكتالوج", "الدورة")])
    row = correct(CorrectionService(catalog, FakeLLM(), aliases=store), "الدورة")[0].cases[0]
    assert row.correctDistrict == "الدورة"


def test_feedback_api_saves_and_forgets(catalog_path, tmp_path, monkeypatch):
    configured = replace(settings, district_database_path=catalog_path, district_api_key="secret",
                         district_alias_database_path=tmp_path / "aliases.sqlite3")
    monkeypatch.setattr(service_router, "settings", configured)
    monkeypatch.setattr(service_auth, "settings", configured)
    body = {"companyName": "FUHOOD", "corrections": [
        {"stateCode": "BGD", "district": "بغداد الدورة ابو دشير", "correctDistrict": "ابو دشير"}]}
    headers = {"X-API-Key": "secret"}
    with TestClient(service_main.app) as client:
        assert client.post("/v1/district-correction/feedback", json=body).status_code == 422
        saved = client.post("/v1/district-correction/feedback", headers=headers, json=body)
        assert saved.status_code == 200 and saved.json()["saved"] == 1
        assert client.get("/v1/district-correction/ready").json()["rememberedCorrections"] == {"reviewer": 1, "auto": 0}
        names = client.get("/v1/district-correction/districts", headers=headers,
                           params={"companyName": "fuhood", "stateCode": "KOT"}).json()
        assert names["stateCode"] == "WST" and "داموك" in names["districts"]
        assert client.get("/v1/district-correction/districts", headers=headers,
                          params={"companyName": "FUHOOD", "stateCode": "XXX"}).status_code == 404
        cases = {"companyName": "FUHOOD", "cases": [{"excelSequence": 1, "stateCode": "BGD",
                                                    "district": "بغداد الدورة ابو دشير"}]}
        row = client.post("/v1/district-correction", headers=headers, json=cases).json()["cases"][0]
        assert row["correctDistrict"] == "ابو دشير"
        removed = client.request("DELETE", "/v1/district-correction/feedback", headers=headers, json=body)
        assert removed.json()["removed"] == 1
        body["companyName"] = "NOPE"
        assert client.post("/v1/district-correction/feedback", headers=headers, json=body).status_code == 404


def test_automatic_learning_never_replaces_a_reviewer(tmp_path):
    store = AliasStore(tmp_path / "a.sqlite3")
    store.learn([("FUHOOD", "BGD", "k", "الدورة", "t")], source="auto")
    store.learn([("FUHOOD", "BGD", "k", "ابو دشير", "t")], source="auto")
    assert store.get("FUHOOD", "BGD", "k") == ("ابو دشير", "auto")
    store.learn([("FUHOOD", "BGD", "k", "الدورة", "t")])
    assert store.learn([("FUHOOD", "BGD", "k", "ابو دشير", "t")], source="auto") == 0
    assert AliasStore(tmp_path / "a.sqlite3").get("FUHOOD", "BGD", "k") == ("الدورة", "reviewer")


def test_old_memory_files_gain_the_source_column(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE district_aliases (company TEXT NOT NULL, state_code TEXT NOT NULL, "
                   "alias_key TEXT NOT NULL, district_name TEXT NOT NULL, sample_text TEXT NOT NULL, "
                   "confirmations INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL, "
                   "PRIMARY KEY (company, state_code, alias_key))")
        db.execute("INSERT INTO district_aliases VALUES ('FUHOOD', 'BGD', 'k', 'الدورة', 't', 1, 'now')")
    assert AliasStore(path).get("FUHOOD", "BGD", "k") == ("الدورة", "reviewer")


def test_llm_answer_agreeing_with_the_rules_is_learned(catalog, tmp_path):
    llm = FakeLLM()  # answers with the rules' suggestion
    service = CorrectionService(catalog, llm, aliases=AliasStore(tmp_path / "a.sqlite3"), auto_learn=True)
    first, metrics = correct(service, "بغداد الكرادة شارع الرشيد، بناية 10")
    assert llm.calls == [[1]] and metrics["learned"] == 1
    second, metrics = correct(service, "بغداد / الكراده شارع الرشيد بناية 10")
    row = second.cases[0]
    assert (row.correctDistrict, row.confidence, row.status) == ("الكرادة", 0.95, "SPLIT_ADDRESS")
    assert row.reason.startswith("Learned") and llm.calls == [[1]] and metrics["remembered"] == 1


def test_llm_answer_differing_from_rules_and_semantic_is_not_learned(catalog, tmp_path):
    llm = FakeLLM(pick="ابو دشير")
    store = AliasStore(tmp_path / "a.sqlite3")
    service = CorrectionService(catalog, llm, aliases=store, auto_learn=True)
    response, metrics = correct(service, "بغداد الدورة ابو دشير شارع الزيتون")
    assert response.cases[0].correctDistrict == "ابو دشير" and metrics["learned"] == 0 and len(store) == 0


def test_llm_answer_agreeing_with_semantic_search_is_learned(catalog, tmp_path):
    llm = FakeLLM(pick="ابو دشير")
    store = AliasStore(tmp_path / "a.sqlite3")
    semantic = FakeSemantic([("ابو دشير", 0.80), ("الدورة", 0.65)])
    service = CorrectionService(catalog, llm, aliases=store, semantic=semantic, auto_learn=True)
    assert correct(service, "بغداد الدورة ابو دشير شارع الزيتون")[1]["learned"] == 1
    assert store.counts() == {"reviewer": 0, "auto": 1}


def test_semantic_agreement_is_learned_and_learning_can_be_disabled(catalog, tmp_path):
    semantic = FakeSemantic([("الكرادة", 0.91), ("شارع الرشيد", 0.80)])
    store = AliasStore(tmp_path / "a.sqlite3")
    off = CorrectionService(catalog, FakeLLM(), aliases=store, semantic=semantic)
    assert correct(off, "بغداد الكرادة شارع الرشيد")[1]["learned"] == 0 and len(store) == 0
    on = CorrectionService(catalog, FakeLLM(), aliases=store, semantic=semantic, auto_learn=True)
    assert correct(on, "بغداد الكرادة شارع الرشيد")[1]["learned"] == 1


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------

def test_semantic_agreement_skips_the_llm(catalog):
    llm = FakeLLM()
    semantic = FakeSemantic([("الكرادة", 0.91), ("شارع الرشيد", 0.80)])
    service = CorrectionService(catalog, llm, semantic=semantic)
    response, metrics = correct(service, "بغداد الكرادة شارع الرشيد، بناية 10")
    row = response.cases[0]
    assert row.correctDistrict == "الكرادة" and row.reason.endswith("Semantic search agrees.")
    assert llm.calls == [] and metrics["semantic_agreed"] == 1
    assert semantic.texts == ["الكرادة شارع الرشيد، بناية 10"]


@pytest.mark.parametrize("ranking", [
    [("ابو دشير", 0.90), ("الدورة", 0.85)],   # another first choice
    [("الدورة", 0.90), ("ابو دشير", 0.89)],   # same first choice without a clear margin
])
def test_semantic_doubt_sends_the_case_to_the_llm_with_similar_names(catalog, ranking):
    llm = FakeLLM()
    service = CorrectionService(catalog, llm, semantic=FakeSemantic(ranking))
    correct(service, "بغداد الدورة ابو دشير شارع الزيتون")
    assert llm.calls == [[1]] and llm.similar == {1: [name for name, _ in ranking]}


def test_semantic_failure_falls_back_to_the_llm(catalog):
    llm = FakeLLM()
    service = CorrectionService(catalog, llm, semantic=FakeSemantic(EmbeddingError("ConnectError")))
    response, metrics = correct(service, "بغداد الكرادة شارع الرشيد")
    assert llm.calls == [[1]] and metrics["semantic_agreed"] == 0
    assert response.cases[0].correctDistrict == "الكرادة"


def test_semantic_index_ranks_by_cosine_and_caches_catalog_vectors(monkeypatch):
    vectors = {"passage: الكرادة": [1, 0], "passage: المنصور": [0, 1],
               "query: كراده داخل": [0.9, 0.1], "query: منصور": [0.2, 0.8]}
    requests = []

    def handler(request):
        payload = json.loads(request.content)
        requests.append((str(request.url), payload["model"], payload["input"]))
        return httpx.Response(200, json={"data": [{"index": i, "embedding": vectors[text]}
                                                  for i, text in reversed(list(enumerate(payload["input"])))]})

    real = httpx.AsyncClient
    monkeypatch.setattr(semantic_module.httpx, "AsyncClient",
                        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    index = SemanticIndex(replace(settings, district_embedding_base_url="http://embed:8001/v1",
                                  district_embedding_model="e5", district_embedding_query_prefix="query: ",
                                  district_embedding_passage_prefix="passage: "))
    allowed = [{"name": "الكرادة"}, {"name": "المنصور"}]
    first = asyncio.run(index.rank("FUHOOD", "BGD", allowed, ["كراده داخل", "منصور", "كراده داخل"]))
    assert [ranking[0][0] for ranking in first] == ["الكرادة", "المنصور", "الكرادة"]
    asyncio.run(index.rank("FUHOOD", "BGD", allowed, ["منصور"]))
    assert requests[0] == ("http://embed:8001/v1/embeddings", "e5", ["passage: الكرادة", "passage: المنصور"])
    assert [inputs for _, _, inputs in requests[1:]] == [["query: كراده داخل", "query: منصور"], ["query: منصور"]]


def test_semantic_index_reports_server_errors(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(semantic_module.httpx, "AsyncClient", lambda **kwargs: real(
        transport=httpx.MockTransport(lambda request: httpx.Response(500)), **kwargs))
    index = SemanticIndex(replace(settings, district_embedding_base_url="http://embed", district_embedding_model="m"))
    with pytest.raises(EmbeddingError):
        asyncio.run(index.rank("FUHOOD", "BGD", [{"name": "الكرادة"}], ["كراده"]))
