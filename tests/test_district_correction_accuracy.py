"""Accuracy suite for district correction: real catalog names written the ways people type them.

Expected answers come from the variant itself (the base catalog name), never from the
matcher. Base names were picked for catalog properties only: no other name in the same
company/state shares their spelling key, label form, or leading words.
"""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from app.config import settings
from app.features.district_correction import llm as llm_module
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.llm import LLMClient, LLMError, parse_cases, response_format
from app.features.district_correction.matching import match_case
from app.features.district_correction.models import CaseRequest, CorrectionRequest
from app.features.district_correction.normalization import (compact_key, loose_key, normalize,
                                                             phrase_key, surface_key, word_key)


@pytest.fixture(scope="module")
def catalog(tmp_path_factory):
    path = tmp_path_factory.mktemp("accuracy") / "districts.sqlite3"
    import_catalog(settings.district_source_dir, path)
    return Catalog(path)


class NoLLM:
    configured = False


class FakeLLM:
    configured = True

    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    async def resolve(self, company, state_code, cases, allowed_names, hints=None):
        self.calls.append([case.excelSequence for case in cases])
        self.allowed_names = allowed_names
        self.hints = hints
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer(cases) if callable(self.answer) else self.answer


def case(sequence, district, address="", state="BGD", state_name=""):
    return CaseRequest(excelSequence=sequence, stateCode=state, stateName=state_name,
                       district=district, address=address)


def correct(catalog, cases, company="ALZAEEM", llm=None):
    service = CorrectionService(catalog, llm or NoLLM())
    return asyncio.run(service.correct(CorrectionRequest(companyName=company, cases=cases)))[0].cases


def one(catalog, district, company, state, address="", state_name="", llm=None):
    return correct(catalog, [case(1, district, address, state, state_name)], company, llm)[0]


# ---------------------------------------------------------------------------
# 1) The reported cases, for every company
# ---------------------------------------------------------------------------

REPORTED = [
    ("ARB", "عنكاوا - خلف فندق هيكسوس", "عينكاوا", "خلف فندق هيكسوس", "SPLIT_ADDRESS"),
    ("BAS", "اسكان الموانئ- شارع الميثاق", "جمعية اسكان الموانئ", "شارع الميثاق", "SPLIT_ADDRESS"),
    ("BGD", "مجمع بوابة الكاظمية", "بوابة الكاظمية", "", "NORMALIZED_MATCH"),
]
COMPANIES = ["ALZAEEM", "FUHOOD", "KHAYAL", "RIYAM", "TEST"]


@pytest.mark.parametrize("company", COMPANIES)
@pytest.mark.parametrize("state, district, expected, details, status", REPORTED,
                         ids=["ankawa", "ports-housing", "kadhimiya-gate"])
def test_reported_examples(catalog, company, state, district, expected, details, status):
    row = one(catalog, district, company, state)
    assert (row.correctDistrict, row.addressDetails, row.status) == (expected, details, status)
    assert row.errorCode is None and row.originalDistrict == district


@pytest.mark.parametrize("company", COMPANIES)
def test_reported_examples_go_to_the_ai_with_the_spelling_suggestion(catalog, company):
    llm = FakeLLM(LLMError("LLM_TIMEOUT"))
    rows = correct(catalog, [case(i, district, state=state) for i, (state, district, *_) in enumerate(REPORTED)],
                   company, llm)
    # "مجمع بوابة الكاظمية" is the catalog name with a label word: certain, no AI call.
    assert sorted(sum(llm.calls, [])) == [0, 1]
    # The AI was unreachable, so the spelling suggestion is kept rather than lost.
    assert [row.correctDistrict for row in rows] == [expected for _, _, expected, *_ in REPORTED]
    assert all("AI check unavailable" in row.reason for row in rows[:2])


@pytest.mark.parametrize("company", COMPANIES)
def test_reported_examples_take_the_ai_answer(catalog, company):
    def answer(cases):
        by_district = {district: (expected, details) for _, district, expected, details, _ in REPORTED}
        return [{"excelSequence": item.excelSequence, "originalDistrict": item.district,
                 "correctDistrict": by_district[item.district][0], "addressDetails": by_district[item.district][1],
                 "stateCode": item.stateCode, "status": "SPLIT_ADDRESS", "reason": "understood"} for item in cases]
    llm = FakeLLM(answer)
    rows = correct(catalog, [case(i, district, state=state) for i, (state, district, *_) in enumerate(REPORTED)],
                   company, llm)
    assert [(row.correctDistrict, row.addressDetails, row.confidence) for row in rows[:2]] == [
        (expected, details, 0.97) for _, _, expected, details, _ in REPORTED[:2]]
    assert [row.reason for row in rows[:2]] == ["understood"] * 2
    assert (rows[2].correctDistrict, rows[2].status) == ("بوابة الكاظمية", "NORMALIZED_MATCH")


# ---------------------------------------------------------------------------
# 2) Real catalog names x the ways they get typed
# ---------------------------------------------------------------------------

BASE_NAMES = [
    ("ALZAEEM", "ARB", "ويفي افيو"), ("ALZAEEM", "BBL", "النبي ايوب"),
    ("ALZAEEM", "DYL", "الحي عماره سلوان"), ("ALZAEEM", "MTH", "الغربي الثانية"),
    ("ALZAEEM", "MYS", "العمارة"), ("ALZAEEM", "NIN", "حمام العليل"),
    ("ALZAEEM", "QAD", "العروبة الاولى"), ("FUHOOD", "ANB", "جزيرة الرمادي"),
    ("FUHOOD", "BGD", "الرستمية"), ("FUHOOD", "KRB", "كلية الصفوة"),
    ("FUHOOD", "MTH", "الحيدرية"), ("FUHOOD", "NJF", "شارع القطعة"),
    ("FUHOOD", "SMH", "الرعاية"), ("KHAYAL", "ANB", "زنكورة"),
    ("KHAYAL", "KRK", "ساحة الطيران"), ("KHAYAL", "MTH", "الاسكان"),
    ("KHAYAL", "MYS", "ابو شطيب"), ("KHAYAL", "QAD", "العروبة الثانية"),
    ("RIYAM", "BAS", "شارع السعدي"), ("RIYAM", "BBL", "شارع ام البنين"),
    ("RIYAM", "BGD", "المسرح الوطني"), ("RIYAM", "DHI", "سوق الشيوخ"),
    ("RIYAM", "DYL", "بني سعد الفلكه"), ("RIYAM", "KRB", "شط الله"),
    ("RIYAM", "MYS", "علي الغربي"), ("RIYAM", "NIN", "مزارع الحدباء"),
    ("RIYAM", "NJF", "معمل البيبسي"), ("RIYAM", "SAH", "الشرقاط"),
    ("RIYAM", "WST", "الزبيدية"), ("TEST", "ANB", "القطانة"),
    ("TEST", "KRB", "الانتفاضة الثانية"), ("TEST", "MTH", "جسر جروخي"),
]
STATE_AR = {"ANB": "الأنبار", "ARB": "أربيل", "BAS": "البصرة", "BBL": "بابل", "BGD": "بغداد",
            "DHI": "ذي قار", "DYL": "ديالى", "KRB": "كربلاء", "KRK": "كركوك", "MTH": "المثنى",
            "MYS": "ميسان", "NIN": "نينوى", "NJF": "النجف", "QAD": "القادسية", "SAH": "صلاح الدين",
            "SMH": "السليمانية", "WST": "واسط"}
DETAIL = "قرب جامع الرحمن خلف المدرسة"


def _swap_final(word):
    return word[:-1] + {"ة": "ه", "ه": "ة", "ى": "ي", "ي": "ى"}.get(word[-1], word[-1])


def _toggle_article(name):
    first, *rest = name.split()
    first = first[2:] if first.startswith("ال") and len(first) > 4 else "ال" + first
    return " ".join([first, *rest])


def _variants(company, state, name):
    """(id, district, address, expected details, allowed statuses)."""
    spelling = {"NORMALIZED_MATCH"}
    state_ar = STATE_AR[state]
    rows = [
        ("exact", name, "", "", {"EXACT_MATCH"}),
        ("final-letter", " ".join(_swap_final(word) for word in name.split()), "", "", spelling),
        ("article", _toggle_article(name), "", "", spelling),
        ("tatweel", name.replace(name[1], name[1] + "ـ", 1), "", "", spelling),
        ("diacritics", name[0] + "َ" + name[1:], "", "", spelling),
        ("spaces", " " + "   ".join(name.split()) + "  ", "", "", spelling | {"EXACT_MATCH"}),
        ("persian-letters", name.replace("ي", "ی").replace("ك", "ک"), "", "", spelling | {"EXACT_MATCH"}),
        ("dash-details", name + " - " + DETAIL, "", DETAIL, {"SPLIT_ADDRESS"}),
        ("comma-details", name + "، " + DETAIL, "", DETAIL, {"SPLIT_ADDRESS"}),
        ("governorate-first", state_ar + " " + name, "", "", spelling),
        ("governorate-label-details", "محافظة " + state_ar + " - " + name + " / " + DETAIL, "", DETAIL,
         {"SPLIT_ADDRESS"}),
        ("governorate-after", name + " " + state_ar + " - " + DETAIL, "", DETAIL, {"SPLIT_ADDRESS"}),
        ("label-first", "منطقة " + name, "", "", spelling),
        ("repeated-in-address", name, name + " " + DETAIL, DETAIL, {"SPLIT_ADDRESS"}),
    ]
    return [pytest.param(company, state, name, district, address, details, statuses,
                         id=f"{company}-{state}-{kind}")
            for kind, district, address, details, statuses in rows
            if kind == "exact" or district != name or address]  # skip variants identical to the name


VARIANTS = [param for company, state, name in BASE_NAMES for param in _variants(company, state, name)]


@pytest.mark.parametrize("company, state, name, district, address, details, statuses", VARIANTS)
def test_catalog_name_variants(catalog, company, state, name, district, address, details, statuses):
    assert name in {item["name"] for item in catalog.districts(company, state)}
    row = one(catalog, district, company, state, address)
    assert row.correctDistrict == name
    assert row.status in statuses
    assert row.addressDetails == details
    assert row.errorCode is None
    assert row.originalDistrict == district


@pytest.mark.parametrize("company, state, name", [pytest.param(*item, id=f"{item[0]}-{item[1]}")
                                                  for item in BASE_NAMES])
def test_typo_is_matched_or_left_to_llm_never_wrong(catalog, company, state, name):
    words = name.split()
    longest = max(range(len(words)), key=lambda i: len(words[i]))
    word = words[longest]
    words[longest] = word[:len(word) // 2] + word[len(word) // 2 + 1:]
    row = one(catalog, " ".join(words) + " - " + DETAIL, company, state)
    assert row.status == "UNRESOLVED" or row.correctDistrict == name


# ---------------------------------------------------------------------------
# 3) Rules that must hold everywhere
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("district, state, company", [
    ("قرب جامع الرحمن", "BGD", "ALZAEEM"),
    ("خلف المدرسة الابتدائية", "BAS", "FUHOOD"),
    ("مقابل مستشفى الولادة", "NJF", "RIYAM"),
    ("بيت ابو علي الطابق الثاني", "KRB", "TEST"),
    ("xyz 123", "BGD", "KHAYAL"),
    ("؟؟؟", "BGD", "ALZAEEM"),
    ("بغداد", "BGD", "ALZAEEM"),
    ("محافظة البصرة", "BAS", "ALZAEEM"),
])
def test_address_only_text_stays_unresolved(catalog, district, state, company):
    row = one(catalog, district, company, state, "قرب السوق")
    assert (row.status, row.correctDistrict, row.addressDetails) == ("UNRESOLVED", district, "قرب السوق")


@pytest.mark.parametrize("district", ["عامود 1234567", "شارع 99999", "حي 777777"])
def test_numbers_are_never_fuzzy_matched(catalog, district):
    for company in COMPANIES:
        for state in ("KRB", "NJF", "BGD"):
            row = one(catalog, district, company, state)
            assert row.status == "UNRESOLVED", (company, state, row.correctDistrict)


@pytest.mark.parametrize("company, state, district, expected", [
    ("RIYAM", "DYL", "الحديد شارع 40", "الحديد شارع ٤٠"),
    ("RIYAM", "DYL", "الحديد شارع ٤٠", "الحديد شارع ٤٠"),
])
def test_eastern_and_western_digits_are_equal(catalog, company, state, district, expected):
    assert one(catalog, district, company, state).correctDistrict == expected


def test_number_mismatch_is_not_a_typo(catalog):
    row = one(catalog, "الحديد - شارع 20 خلف المدرسة", "RIYAM", "DYL")
    assert row.correctDistrict != "الحديد شارع ٤٠"


@pytest.mark.parametrize("company, state, district", [
    ("ALZAEEM", "BBL", "حي بابل حسين"),        # "حي بابل" + "حسين" or "بابل حسين"
    ("ALZAEEM", "BGD", "حي الشرطة الخامسة"),   # "حي الشرطة" or "الشرطة الخامسة"
    ("ALZAEEM", "DYL", "بني سعد الحي العكري"),  # العسكري or العصري
    ("ALZAEEM", "DYL", "بني سعد الجسيه"),       # الجنسيه or السميه
])
def test_two_readings_go_to_llm_instead_of_guessing(catalog, company, state, district):
    row = one(catalog, district, company, state)
    assert row.status == "UNRESOLVED" and row.correctDistrict == district


@pytest.mark.parametrize("company, state, district, expected", [
    ("KHAYAL", "SMH", "گولي  شار", "گولي شار"),
    ("KHAYAL", "SMH", "كولي  شار", "كولي شار"),
    ("RIYAM", "MTH", "الرميثه ", "الرميثة"),
    ("KHAYAL", "MTH", "رميثـه", "رميثه"),
])
def test_duplicate_catalog_spellings_pick_the_closest(catalog, company, state, district, expected):
    names = {item["name"] for item in catalog.districts(company, state)}
    assert expected in names
    assert one(catalog, district, company, state).correctDistrict == expected


@pytest.mark.parametrize("company, state, district, expected, details", [
    ("KHAYAL", "WST", "قضاء الموفقيه واسط حي الزهراء", "الموفقية", "حي الزهراء"),
    ("KHAYAL", "WST", "واسط قضاء الموفقيه حي الزهراء", "الموفقية", "حي الزهراء"),
    ("KHAYAL", "WST", "الموفقية واسط حي الزهراء", "الموفقية", "حي الزهراء"),
    ("FUHOOD", "BGD", "بغداد الكرادة", "الكرادة", ""),
    ("FUHOOD", "BGD", "بغداد - الكرادة - شارع 62", "الكرادة", "شارع 62"),
    ("FUHOOD", "DHI", "ذي قار الشطرة قرب السوق", "الشطرة", "قرب السوق"),
    ("ALZAEEM", "BGD", "الكرادة شارع الرشيد بغداد", "الكرادة", "شارع الرشيد"),
    ("KHAYAL", "WST", "واسط", "واسط", ""),
    ("FUHOOD", "BGD", "بغداد الجديدة", "بغداد الجديدة", ""),
])
def test_governorate_words_are_removed_not_matched(catalog, company, state, district, expected, details):
    row = one(catalog, district, company, state)
    assert (row.correctDistrict, row.addressDetails) == (expected, details)


@pytest.mark.parametrize("company, state, district, expected, details", [
    ("ALZAEEM", "ANB", "عنه - حي التخي - قرب المدرسة", "عنه - حي التاخي", "قرب المدرسة"),
    ("ALZAEEM", "BAS", "جامعة البصره - قرب الجامع", "جامعة البصرة", "قرب الجامع"),
    ("ALZAEEM", "ANB", "الفلوجه - حي الشهداء", "الفلوجة - حي الشهداء", ""),
    ("ALZAEEM", "BGD", "الادريسي الكرادة", "الادريسي / الكرادة", ""),
    ("ALZAEEM", "NJF", "مجمع الامرات الثاني", "مجمع الاميرات الثاني", ""),
])
def test_longer_catalog_names_win_over_their_prefix(catalog, company, state, district, expected, details):
    row = one(catalog, district, company, state)
    assert (row.correctDistrict, row.addressDetails) == (expected, details)


def test_company_catalogs_stay_isolated(catalog):
    for company in COMPANIES:
        for state in ("BGD", "BAS", "KRB"):
            names = {item["name"] for item in catalog.districts(company, state)}
            for district in ("الكرادة شارع 5", "العشار - قرب السوق", "حي الحسين"):
                row = one(catalog, district, company, state)
                assert row.status == "UNRESOLVED" or row.correctDistrict in names


# ---------------------------------------------------------------------------
# 4) stateName handling
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("state_name", ["", "بغداد", "BAGHDAD", "baghdad", "Baghdad", " بغداد ",
                                        "بغدا", "Baghdad City", "BGD"])
def test_state_name_that_agrees_or_is_unknown_keeps_working(catalog, state_name):
    row = one(catalog, "الكرادة", "FUHOOD", "BGD", state_name=state_name)
    assert (row.status, row.errorCode) == ("EXACT_MATCH", None)


@pytest.mark.parametrize("state_name, code", [("ذي قار", "DHI"), ("DHI_QAR", "DHI"), ("dhi qar", "DHI"),
                                              ("ذيقار", "DHI"), ("البصره", "BAS"), ("بصرة", "BAS"),
                                              ("SALAH_AL_DIN", "SAH"), ("صلاح الدين", "SAH")])
def test_state_name_spellings_are_recognized(catalog, state_name, code):
    assert catalog.state_code_for(state_name) == code


@pytest.mark.parametrize("state_name", ["البصرة", "NAJAF", "كربلاء"])
def test_state_name_for_another_governorate_is_rejected(catalog, state_name):
    row = one(catalog, "الكرادة", "FUHOOD", "BGD", state_name=state_name)
    assert (row.status, row.errorCode) == ("UNRESOLVED", "UNKNOWN_STATE")


# ---------------------------------------------------------------------------
# 5) Normalization
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("first, second", [
    ("الكرادة", "الكراده"), ("الكرادة", "كرادة"), ("المنصور", "منصور"), ("أربيل", "اربيل"),
    ("إسكان", "اسكان"), ("آمرلي", "امرلي"), ("الموانئ", "الموانى"), ("الموانئ", "الموانيء"),
    ("كربلاء", "كربلا"), ("مستشفى", "مستشفي"), ("کربلاء", "كربلاء"), ("الحی", "الحي"),
    ("گال", "كال"), ("باچر", "باجر"),
    ("الكـرادة", "الكرادة"), ("الكَرّادة", "الكرادة"),
    ("شارع ٤٠", "شارع 40"), ("شارع ۴۰", "شارع 40"), ("الادريسي / الكرادة", "الادريسي الكرادة"),
    ("ابو غريب - الشيحه", "ابو غريب الشيحه"), ("بيتاسي.دهوك", "بيتاسي دهوك"), ("  الكرادة  ", "الكرادة"),
])
def test_phrase_keys_treat_spellings_alike(first, second):
    assert phrase_key(first) == phrase_key(second)


@pytest.mark.parametrize("first, second", [
    ("الكرادة", "المنصور"), ("شارع 40", "شارع 20"), ("حي الجامعة", "الجامعة"),
    ("الامين الاولى", "الامين الثانية"), ("بغداد الجديدة", "بغداد"),
])
def test_phrase_keys_keep_different_places_apart(first, second):
    assert phrase_key(first) != phrase_key(second)


@pytest.mark.parametrize("first, second", [("عبد الله", "عبدالله"), ("ابو غريب", "ابوغريب"),
                                           ("زيونة", "زيونه")])
def test_compact_key_ignores_spaces(first, second):
    assert compact_key(first) == compact_key(second)


def test_surface_key_keeps_letters_as_written():
    assert surface_key("باچر  الصبح") == "باچر الصبح"
    assert surface_key("باچر الصبح") != surface_key("باجر الصبح")
    assert surface_key("الكـرادة") == "الكرادة"


def test_word_key_and_loose_key():
    assert word_key("الكرادة") == "كراده"
    assert word_key("الحي") == "الحي"  # too short to lose the article
    assert loose_key("عينكاوا") == loose_key("عينكاوة".replace("ة", "ه"))
    assert normalize("DHI_QAR") == "dhi qar"


# ---------------------------------------------------------------------------
# 6) The LLM client
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, status, content):
        self.status_code = status
        self._content = content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=httpx.Request("POST", "http://x"),
                                        response=httpx.Response(self.status_code))

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def _client_with(monkeypatch, responses):
    sent = []

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, json=None, headers=None):
            sent.append({"url": url, "json": json, "headers": headers})
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(llm_module.httpx, "AsyncClient", Client)
    return sent


def _resolve(base_url="http://localhost:1234/v1", key=""):
    client = LLMClient(replace(settings, district_llm_base_url=base_url, district_llm_model="m",
                               district_llm_api_key=key))
    return asyncio.run(client.resolve("FUHOOD", "DHI", [case(1, "شطره", state="DHI")], ["الشطرة", "الناصرية"]))


ANSWER = json.dumps({"cases": [{"excelSequence": 1, "correctDistrict": "الشطرة", "status": "AI_MATCH"}]},
                    ensure_ascii=False)


def test_llm_request_uses_json_schema_limited_to_catalog_names(monkeypatch):
    sent = _client_with(monkeypatch, [_Response(200, ANSWER)])
    assert _resolve()[0]["correctDistrict"] == "الشطرة"
    body = sent[0]["json"]
    assert body["response_format"]["type"] == "json_schema"
    item = body["response_format"]["json_schema"]["schema"]["properties"]["cases"]["items"]
    assert set(item["properties"]["correctDistrict"]["enum"]) == {"الشطرة", "الناصرية", "شطره"}
    assert item["properties"]["status"]["enum"] == ["AI_MATCH", "SPLIT_ADDRESS", "UNRESOLVED"]
    assert body["max_tokens"] >= 1024 and body["temperature"] == 0
    assert body["reasoning_effort"] == "none"
    assert body["chat_template_kwargs"] == {"enable_thinking": False}


@pytest.mark.parametrize("status", [400, 422])
def test_llm_retries_without_schema_when_server_rejects_it(monkeypatch, status):
    sent = _client_with(monkeypatch, [_Response(status, ""), _Response(200, ANSWER)])
    assert _resolve()[0]["excelSequence"] == 1
    assert "response_format" in sent[0]["json"] and "response_format" not in sent[1]["json"]
    assert sent[0]["json"]["reasoning_effort"] == sent[1]["json"]["reasoning_effort"] == "none"
    assert sent[0]["json"]["chat_template_kwargs"] == sent[1]["json"]["chat_template_kwargs"] == {
        "enable_thinking": False}


@pytest.mark.parametrize("base_url, expected", [
    ("http://localhost:1234/v1", "http://localhost:1234/v1/chat/completions"),
    ("http://localhost:1234/v1/", "http://localhost:1234/v1/chat/completions"),
    ("http://localhost:1234", "http://localhost:1234/v1/chat/completions"),
    ("http://host/v1/chat/completions", "http://host/v1/chat/completions"),
])
def test_llm_url_building(monkeypatch, base_url, expected):
    sent = _client_with(monkeypatch, [_Response(200, ANSWER)])
    _resolve(base_url)
    assert sent[0]["url"] == expected


def test_llm_api_key_is_sent_only_when_configured(monkeypatch):
    sent = _client_with(monkeypatch, [_Response(200, ANSWER), _Response(200, ANSWER)])
    _resolve(key="secret")
    _resolve()
    assert sent[0]["headers"] == {"Authorization": "Bearer secret"} and sent[1]["headers"] == {}


@pytest.mark.parametrize("error, code", [
    (httpx.ReadTimeout("slow"), "LLM_TIMEOUT"),
    (httpx.ConnectError("down"), "LLM_INVALID_RESPONSE"),
])
def test_llm_transport_errors_are_reported(monkeypatch, error, code):
    _client_with(monkeypatch, [error])
    with pytest.raises(LLMError, match=code):
        _resolve()


def test_llm_server_error_is_reported(monkeypatch):
    _client_with(monkeypatch, [_Response(500, "")])
    with pytest.raises(LLMError, match="LLM_INVALID_RESPONSE"):
        _resolve()


@pytest.mark.parametrize("content", [
    ANSWER,
    "```json\n" + ANSWER + "\n```",
    "<think>let me check the list</think>" + ANSWER,
    "Here is the result:\n" + ANSWER + "\nDone.",
    json.dumps([{"excelSequence": 1, "correctDistrict": "الشطرة", "status": "AI_MATCH"}], ensure_ascii=False),
])
def test_parse_cases_tolerates_model_formatting(content):
    assert parse_cases(content)[0]["correctDistrict"] == "الشطرة"


@pytest.mark.parametrize("content", ["", "   ", None, "not json", '{"other": []}', "{\"cases\": 5}"])
def test_parse_cases_rejects_non_answers(content):
    with pytest.raises(ValueError):
        parse_cases(content)


def test_response_format_includes_original_for_unresolved():
    schema = response_format(["أ"], [case(1, "مكان مجهول")])
    names = schema["json_schema"]["schema"]["properties"]["cases"]["items"]["properties"]["correctDistrict"]["enum"]
    assert "مكان مجهول" in names


# ---------------------------------------------------------------------------
# 7) Service behaviour around the LLM
# ---------------------------------------------------------------------------

def test_llm_failure_keeps_spelling_matches(catalog):
    cases = [case(1, "شطره", state="DHI"), case(2, "الكرادة شارع الرشيد، بناية 10"),
             case(3, "مكان مجهول تماما", "قرب الجامع")]
    llm = FakeLLM(LLMError("LLM_INVALID_RESPONSE"))
    rows = correct(catalog, cases, "FUHOOD", llm)
    assert [(row.correctDistrict, row.status) for row in rows[:2]] == [
        ("الشطرة", "NORMALIZED_MATCH"), ("الكرادة", "SPLIT_ADDRESS")]
    assert rows[1].addressDetails == "شارع الرشيد، بناية 10"
    assert rows[0].errorCode is None and "LLM_INVALID_RESPONSE" in rows[1].reason
    assert (rows[2].status, rows[2].errorCode) == ("UNRESOLVED", "LLM_INVALID_RESPONSE")
    # "شطره" is the catalog name up to spelling: certain, so it never waits on the AI.
    assert sorted(sum(llm.calls, [])) == [2, 3]


def test_only_uncertain_cases_reach_the_ai(catalog):
    llm = FakeLLM([])
    correct(catalog, [case(1, "الكرادة"), case(2, "الكراده"), case(3, "مكان مجهول"), case(4, "بغداد الكرادة"),
                      case(5, "مكان مجهول اخر"), case(6, "الكرادة", "الكرادة قرب الجامع"),
                      case(7, "الكرادة شارع الرشيد")], "FUHOOD", llm)
    # Exact, spelling-only and governorate-word matches are certain; a split is not.
    assert llm.calls == [[3, 5, 7]]
    assert llm.hints == {7: "الكرادة"}


def test_large_requests_route_the_same_way(catalog):
    llm = FakeLLM([])
    cases = [case(number, "الكراده") for number in range(150)] + [case(150, "الكرادة شارع الرشيد")]
    correct(catalog, cases, "FUHOOD", llm)
    assert sum(llm.calls, []) == [150]


def test_ai_decides_an_uncertain_match(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district,
                 "correctDistrict": "دورة ميكانيك واسيا", "addressDetails": "", "stateCode": "BGD",
                 "status": "SPLIT_ADDRESS", "reason": "meaning"}]
    row = one(catalog, "بغداد الدورة الميكانيك", "FUHOOD", "BGD", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.status, row.confidence) == ("دورة ميكانيك واسيا", "SPLIT_ADDRESS", 0.9)


def test_ai_center_pick_does_not_replace_the_district_after_it(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "السماوة",
                 "addressDetails": "قضاء الخضر", "stateCode": "MTH", "status": "SPLIT_ADDRESS", "reason": "center"}]
    row = one(catalog, "السماوة المثنئ قضاء الخضر مستشفى الخضر العام", "FUHOOD", "MTH", llm=FakeLLM(answer))
    assert row.correctDistrict == "الخضر"


# The AI pick shares its first words with the text, which then names another place.
@pytest.mark.parametrize("state, district, pick, kept", [
    ("BGD", "بغداد الدورة ابو دشير شارع الزيتون", "دورة أبو طيارة", "الدورة"),
    ("DYL", "ديالى بعقوبه التحرير الشارع الحولي", "بعقوبه التربيه", "التحرير"),
])
def test_ai_pick_contradicted_by_the_text_is_rejected(catalog, state, district, pick, kept):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": pick,
                 "addressDetails": "", "stateCode": state, "status": "AI_MATCH", "reason": "guess"}]
    row = one(catalog, district, "FUHOOD", state, llm=FakeLLM(answer))
    assert row.correctDistrict == kept and "differs from the place written" in row.reason


# Reported FUHOOD rows: compound centers, Baghdad's banks, glued labels and two-part names.
@pytest.mark.parametrize("state, district, expected", [
    ("DHI", "الناصرية ذي قار الشطره حي الباقر", "الشطرة"),
    ("ANB", "الانبار الرمادي الشركه قرب اعدادية المتفوقين", "الشركة"),
    ("BGD", "بغداد  الرصافة.. البنوك.. شارع المشاتل", "حي البنوك"),
    ("BGD", "بغداد الكرخ منطقه الداوددي مقابيل جمعية الداوددي", "الداودي"),
    ("SAH", "صلاح الدين/ قضاءالدور/ ناحية المجمع السكني", "الدور"),
    ("BGD", "بغداد اليرموك / الداخلية", "حي الداخلية / اليرموك"),
    ("BGD", "بغداد / اليرموك الداخليه فرع ثانوية حطين", "حي الداخلية / اليرموك"),
    ("NIN", "الموصل حي الشركة", "موصل"),
    ("NIN", "الموصل نينوى &apos;سنجار &apos;مزار شرفدين", "سنجار"),
    ("KRK", "كركوك نور ستي الثانيه", "نور ستي2"),
    ("DHI", "ناصرية _ المدينة _ قرب مدرسة ابن ماجد", "المدينة"),
    ("SAH", "صلاح الدين تكريت القادسيه حي الجامعه", "تكريت قادسية"),
])
def test_reported_fuhood_rows(catalog, state, district, expected):
    assert one(catalog, district, "FUHOOD", state).correctDistrict == expected


# ALZAEEM's catalog names areas as "parent - child" and often has no bare parent.
@pytest.mark.parametrize("state, district, address, expected", [
    ("DHI", "الناصريه / الشطره", "", "قضاء الشطرة"),
    ("DHI", "اخرى", "ناصرية // ناحية الفضلية / قرب المستوصف النموذجي\r\n IQ-DQ-AR \r\nIraq", "الفضلية"),
    ("BAS", "البصره / ابي الخصيب", "", "ابو الخصيب"),
    ("BGD", "الدورة - حي الوادي", "", "الدورة - الوادي"),
    ("BBL", "بابل _ حلة _ جمعية", "", "الجمعية"),
    ("NJF", "النجف الاشرف / حي النفط", "", "حي النفط"),
    ("SAH", "تكريت - القادسية - حي الشهداء", "", "القادسية"),
])
def test_reported_alzaeem_rows(catalog, state, district, address, expected):
    assert one(catalog, district, "ALZAEEM", state, address).correctDistrict == expected


@pytest.mark.parametrize("district", ["بغداد / الدوره", "بغداد / مدينه الصدر"])
def test_parent_area_with_many_catalog_children_stays_unresolved(catalog, district):
    assert one(catalog, district, "ALZAEEM", "BGD").status == "UNRESOLVED"



def test_ai_agreeing_with_a_review_suggestion_is_not_boosted(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "بغداد",
                 "addressDetails": "", "stateCode": "BGD", "status": "AI_MATCH", "reason": "kept"}]
    row = one(catalog, "بغداد الرضوانيه", "FUHOOD", "BGD", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.status, row.confidence) == ("بغداد", "AI_MATCH", 0.85)


@pytest.mark.parametrize("supplied", ["البصره قضاء", "محافظة البصرة", "قضاء"])
def test_ai_details_without_location_are_dropped(catalog, supplied):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "ابو الخصيب",
                 "addressDetails": supplied, "stateCode": "BAS", "status": "SPLIT_ADDRESS", "reason": "town"}]
    row = one(catalog, "البصره قضاء ابي الخصيب", "FUHOOD", "BAS", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.addressDetails) == ("ابو الخصيب", "")


# The center is context; the district written after it is the answer.
@pytest.mark.parametrize("state, district, expected, details", [
    ("DHI", "الناصريه الشطره", "الشطرة", ""),
    ("DHI", "الناصريه الجبايش", "الجبايش", ""),
    ("DHI", "الناصريه قضاء الغراف", "الغراف", ""),
    ("DHI", "ذي قار الشطره شارع زكي الخياط", "الشطرة", "شارع زكي الخياط"),
    ("BBL", "الحله المسيب", "المسيب", ""),
    ("BBL", "حله شوملي", "الشوملي", ""),
    ("NIN", "موصل كوكجلي", "كوكجلي", ""),
    ("QAD", "الديوانيه قضاء غماس", "غماس", ""),
    ("QAD", "ديوانيه سنيه", "السنية", ""),
    ("MYS", "الاعماره ابو رمانه", "ابو رمانة", ""),
    ("NJF", "النجف الاشرف حي الجامعه", "حي الجامعة", ""),
    ("ARB", "اربيل عين كاوه", "عينكاوا", ""),
    # one budgeted typo after the center
    ("KRB", "كربلاء طوريج", "طويريج", ""),
    ("NIN", "الموصل التكليف", "تلكيف", ""),
    ("MYS", "الاعماره العابجيه", "العبجيه", ""),
    # the only catalog name starting with the written words
    ("KRB", "كربلاء حي الامن", "حي الامن الداخلي", ""),
])
def test_district_after_governorate_center(catalog, state, district, expected, details):
    row = one(catalog, district, "FUHOOD", state)
    assert (row.correctDistrict, row.addressDetails) == (expected, details)
    assert row.confidence >= 0.9


@pytest.mark.parametrize("state, district, center", [
    ("BGD", "بغداد الرضوانيه", "بغداد"),      # الرحمانية is only a fuzzy look-alike
])
def test_center_with_lookalike_rest_is_kept_for_ai_review(catalog, state, district, center):
    row = one(catalog, district, "FUHOOD", state)
    assert row.correctDistrict == center and row.confidence < 0.9


@pytest.mark.parametrize("state, district, expected, details", [
    ("BGD", "الكرادة شارع الرشيد، بناية 10", "الكرادة", "شارع الرشيد، بناية 10"),
    ("BGD", "البلديات تقاطع الكهرباء", "البلديات", "تقاطع الكهرباء"),
])
def test_area_before_a_street_stays_the_district(catalog, state, district, expected, details):
    row = one(catalog, district, "FUHOOD", state)
    assert (row.correctDistrict, row.addressDetails) == (expected, details)


def test_ai_agreeing_with_the_suggestion_raises_confidence(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "الكرادة",
                 "addressDetails": "", "stateCode": "BGD", "status": "AI_MATCH", "reason": "same"}]
    row = one(catalog, "الكرادة شارع الرشيد", "FUHOOD", "BGD", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.confidence) == ("الكرادة", 0.97)


def test_center_before_the_district_is_context_and_needs_no_ai(catalog):
    llm = FakeLLM([])
    row = one(catalog, "الناصريه الشطره", "FUHOOD", "DHI", llm=llm)
    assert (row.correctDistrict, row.status) == ("الشطرة", "NORMALIZED_MATCH") and llm.calls == []


def test_ai_unresolved_falls_back_to_the_suggestion(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "status": "UNRESOLVED"}]
    row = one(catalog, "الناصريه الشطره", "FUHOOD", "DHI", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.status, row.errorCode) == ("الشطرة", "NORMALIZED_MATCH", None)


def test_ai_answer_outside_excel_falls_back_to_the_suggestion(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "correctDistrict": "مدينة مخترعة", "status": "AI_MATCH"}]
    row = one(catalog, "شطره", "FUHOOD", "DHI", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.errorCode) == ("الشطرة", None)


@pytest.mark.parametrize("district, expected", [
    ("عامود 1005", "عامود 1005"),
    ("حي الحسين قرب عامود ١٠٠٥", "عامود 1005"),
])
def test_long_catalogs_shortlist_keeps_the_right_name(catalog, district, expected):
    service = CorrectionService(catalog, NoLLM())
    allowed = catalog.districts("ALZAEEM", "KRB")
    assert len(allowed) > 150 and expected in {item["name"] for item in allowed}
    names = service._candidates([case(1, district, state="KRB")], allowed, {})
    assert expected in names and len(names) <= 30


def test_shortlist_contains_the_spelling_suggestion(catalog):
    allowed = catalog.districts("ALZAEEM", "KRB")
    target = next(item["name"] for item in allowed if item["name"].startswith("حي"))
    suggestion = one(catalog, target, "ALZAEEM", "KRB")
    names = CorrectionService._candidates([case(1, "نص لا علاقة له", state="KRB")], allowed, {1: suggestion})
    assert target in names


# Not resolved by the rules (the district is not at the start), but the text names
# الكرادة, so an AI pick of it passes the text-support check.
MENTIONED = "مكان مجهول قرب الكرادة"


def _answer(**fields):
    def build(cases):
        return [dict({"excelSequence": case.excelSequence, "originalDistrict": case.district,
                      "correctDistrict": "الكرادة", "addressDetails": "قرب الجامع", "stateCode": "BGD",
                      "status": "AI_MATCH", "reason": "picked"}, **fields) for case in cases]
    return build


@pytest.mark.parametrize("fields, expected", [
    ({}, ("الكرادة", "AI_MATCH")),
    ({"status": "ai_match"}, ("الكرادة", "AI_MATCH")),
    ({"status": "SPLIT_ADDRESS"}, ("الكرادة", "SPLIT_ADDRESS")),
    ({"correctDistrict": "الكراده"}, ("الكرادة", "AI_MATCH")),
    ({"correctDistrict": "كرادة"}, ("الكرادة", "AI_MATCH")),
    ({"stateCode": "bgd"}, ("الكرادة", "AI_MATCH")),
    ({"stateCode": None}, ("الكرادة", "AI_MATCH")),
    ({"originalDistrict": None}, ("الكرادة", "AI_MATCH")),
    ({"originalDistrict": "  مكان   مجهول قرب  الكرادة "}, ("الكرادة", "AI_MATCH")),
    ({"reason": None}, ("الكرادة", "AI_MATCH")),
])
def test_valid_llm_answers_are_accepted(catalog, fields, expected):
    row = one(catalog, MENTIONED, "FUHOOD", "BGD", llm=FakeLLM(_answer(**fields)))
    assert (row.correctDistrict, row.status) == expected
    assert row.addressDetails == "قرب الجامع" and row.errorCode is None


@pytest.mark.parametrize("fields", [
    {"correctDistrict": "حي مخترع"},
    {"correctDistrict": 7},
    {"stateCode": "BAS"},
    {"originalDistrict": "نص آخر"},
    {"status": "MAYBE"},
    {"status": "SPLIT_ADDRESS", "addressDetails": None},
    {"addressDetails": 5},
])
def test_invalid_llm_answers_are_rejected(catalog, fields):
    row = one(catalog, "مكان مجهول", "FUHOOD", "BGD", llm=FakeLLM(_answer(**fields)))
    assert (row.status, row.errorCode, row.correctDistrict) == ("UNRESOLVED", "LLM_INVALID_RESPONSE", "مكان مجهول")


@pytest.mark.parametrize("district, chosen", [
    ("البصره", "الزبير"), ("بغداد الرضوانيه", "الراشدية"), ("الانبار حي الشرطه", "حي الاندلس"),
])
def test_ai_pick_not_written_in_the_text_is_rejected(catalog, district, chosen):
    state = {"البصره": "BAS", "بغداد الرضوانيه": "BGD", "الانبار حي الشرطه": "ANB"}[district]
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": chosen,
                 "addressDetails": "", "stateCode": state, "status": "AI_MATCH", "reason": "guess"}]
    row = one(catalog, district, "FUHOOD", state, llm=FakeLLM(answer))
    # The rules' own answer (or the original text) is kept, never the unwritten pick.
    assert row.correctDistrict != chosen and row.errorCode is None
    assert "not written in the text" in row.reason


def test_ai_pick_written_with_typos_is_accepted(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "طوزخرماتو",
                 "addressDetails": "", "stateCode": "KRK", "status": "AI_MATCH", "reason": "same town"}]
    row = one(catalog, "كركوك طوز خورماتو", "FUHOOD", "KRK", llm=FakeLLM(answer))
    assert (row.status, row.correctDistrict) == ("AI_MATCH", "طوزخرماتو")


def test_llm_unresolved_keeps_input(catalog):
    row = one(catalog, "مكان مجهول", "FUHOOD", "BGD", "قرب الجامع", llm=FakeLLM(_answer(status="UNRESOLVED")))
    assert (row.status, row.errorCode, row.addressDetails) == ("UNRESOLVED", None, "قرب الجامع")


def test_partial_llm_answer_only_fails_missing_cases(catalog):
    def build(cases):
        return _answer()(cases[:1]) + [{"excelSequence": 999, "status": "AI_MATCH"}, "junk"]
    rows = correct(catalog, [case(1, MENTIONED), case(2, "مكان مجهول اخر")], "FUHOOD", FakeLLM(build))
    assert (rows[0].correctDistrict, rows[0].status) == ("الكرادة", "AI_MATCH")
    assert (rows[1].status, rows[1].errorCode) == ("UNRESOLVED", "LLM_INVALID_RESPONSE")


def test_non_list_llm_answer_is_rejected(catalog):
    row = one(catalog, "مكان مجهول", "FUHOOD", "BGD", llm=FakeLLM({"cases": []}))
    assert row.errorCode == "LLM_INVALID_RESPONSE"


def test_match_case_never_leaves_the_supplied_list(catalog):
    allowed = [{"name": "الكرادة"}, {"name": "المنصور"}]
    for district in ("الكراده شارع 5", "منصور", "عنكاوا", "مجمع بوابة الكاظمية", "بغداد الكرادة"):
        row = match_case(case(1, district), allowed, ("بغداد", "BAGHDAD"))
        assert row.status == "UNRESOLVED" or row.correctDistrict in {"الكرادة", "المنصور"}


@pytest.mark.parametrize("state, district", [
    ("BGD", "جسر ديالى"),     # الجديد and القديم: two places start with these words
    ("BAS", "البصره المربد"),  # المربد الجديد / المربد القديمة
    ("BAS", "البصره"),         # a governorate alone never extends to "البصرة القديمة"
])
def test_prefix_of_several_places_or_a_governorate_stays_unresolved(catalog, state, district):
    assert one(catalog, district, "FUHOOD", state).status == "UNRESOLVED"


def test_misspelled_governorate_goes_to_the_ai(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "المنصور",
                 "addressDetails": "", "stateCode": "BGD", "status": "AI_MATCH", "reason": "بغدد is بغداد"}]
    llm = FakeLLM(answer)
    row = one(catalog, "بغدد المنصور", "FUHOOD", "BGD", llm=llm)
    assert llm.calls == [[1]] and row.correctDistrict == "المنصور"


def test_article_does_not_raise_the_typo_budget(catalog):
    # "رضوانيه" is two letters away from "رحمانيه": over a 7-letter word's budget.
    row = one(catalog, "الرضوانيه", "FUHOOD", "BGD")
    assert row.correctDistrict != "الرحمانية"


def test_honorific_is_dropped_from_ai_details(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "حي الجامعة",
                 "addressDetails": "الاشرف", "stateCode": "NJF", "status": "SPLIT_ADDRESS", "reason": "area"}]
    row = one(catalog, "النجف الاشرف حي الجامعه", "FUHOOD", "NJF", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.addressDetails) == ("حي الجامعة", "")


def _ai(pick, details="", status="SPLIT_ADDRESS"):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": pick,
                 "addressDetails": details, "stateCode": cases[0].stateCode, "status": status, "reason": "llm"}]
    return FakeLLM(answer)


def test_ai_longer_name_with_only_unwritten_words_is_rejected(catalog):
    row = one(catalog, "بغداد شارع فلسطين تقاطع الصخرة", "FUHOOD", "BGD", llm=_ai("الادريسي / شارع فلسطين"))
    assert row.correctDistrict == "شارع فلسطين" and "adds words" in row.reason


def test_ai_pick_written_only_inside_a_landmark_is_rejected(catalog):
    row = one(catalog, "البصره مقابيل المركز الشرطه قرب مستشفى العام البصره", "FUHOOD", "BAS",
              llm=_ai("حي الشرطة"))
    assert row.status == "UNRESOLVED" and "landmark" in row.reason


@pytest.mark.parametrize("state, district, pick, details, expected", [
    ("BGD", "بغداد حي الإعلام الشباب بالقرب من جامع الحبيب المصطفى", "الاعلام",
     "حي الإعلام الشباب بالقرب من جامع الحبيب المصطفى", "الشباب بالقرب من جامع الحبيب المصطفى"),
    ("BGD", "بغداد مجمع بوابة العراق / مقابل متنزه الزوراء عمارة 15", "بوابة العراق",
     "مجمع بوابة العراق / مقابل متنزه الزوراء عمارة 15", "مقابل متنزه الزوراء عمارة 15"),
    # A street named like the district stays.
    ("BAS", "البصره شارع الجزائر قرب مستشفى الموسوي", "الجزائر",
     "شارع الجزائر قرب مستشفى الموسوي", "شارع الجزائر قرب مستشفى الموسوي"),
    # Another governorate's name at the end is part of a landmark here.
    ("SAH", "صلاح الدين تكريت القادسيه شارع جليل القصاب خلف مركز شرطة القادسية", "تكريت قادسية",
     "شارع جليل القصاب خلف مركز شرطة القادسية", "شارع جليل القصاب خلف مركز شرطة القادسية"),
])
def test_ai_details_drop_the_repeated_district_only(catalog, state, district, pick, details, expected):
    assert one(catalog, district, "FUHOOD", state, llm=_ai(pick, details)).addressDetails == expected


# A replaced letter in a short word: a slip only on a neighbouring key or the last letter.
@pytest.mark.parametrize("company, state, district, expected", [
    ("RIYAM", "ARB", "اربيل زاتكو ٩٤", "زانكو"),            # ت/ن neighbouring keys
    ("RIYAM", "BGD", "بغداد زيزنه", "زيونة"),               # ز/و neighbouring keys
    ("RIYAM", "DHI", "ذي قار الشكره", "الشطرة"),            # ك/ط neighbouring keys
])
def test_keyboard_slips_in_short_words_are_corrected(catalog, company, state, district, expected):
    assert one(catalog, district, company, state).correctDistrict == expected


@pytest.mark.parametrize("company, state, district, wrong", [
    ("RIYAM", "QAD", "الديوانية شركة الفنجان شارع الاطارات", "حي الشرطة"),   # a facility word
    ("RIYAM", "BAS", "البصرة - حي حطين قرب جمعية الفواطم", "حي الحسين"),    # ط/س far apart
    ("RIYAM", "WST", "واسط - مزرعة الصويرة", "المزركة"),
])
def test_other_real_words_are_not_typo_corrected(catalog, company, state, district, wrong):
    assert one(catalog, district, company, state).correctDistrict != wrong


def test_center_before_words_shared_by_several_names_is_kept_for_review(catalog):
    row = one(catalog, "بغداد - رصافة معلمين", "KHAYAL", "BGD")
    assert row.correctDistrict == "بغداد" and row.confidence < 0.9
