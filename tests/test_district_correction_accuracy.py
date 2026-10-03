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
    assert sorted(sum(llm.calls, [])) == [0, 1, 2]
    # The AI was unreachable, so the spelling suggestion is kept rather than lost.
    assert [row.correctDistrict for row in rows] == [expected for _, _, expected, *_ in REPORTED]
    assert all("AI check unavailable" in row.reason for row in rows)


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
    assert [(row.correctDistrict, row.addressDetails, row.confidence) for row in rows] == [
        (expected, details, 0.97) for _, _, expected, details, _ in REPORTED]
    assert [row.reason for row in rows] == ["understood"] * 3


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
    assert rows[0].errorCode is None and "LLM_INVALID_RESPONSE" in rows[0].reason
    assert (rows[2].status, rows[2].errorCode) == ("UNRESOLVED", "LLM_INVALID_RESPONSE")
    assert sorted(sum(llm.calls, [])) == [1, 2, 3]


def test_everything_except_literal_matches_reaches_the_ai(catalog):
    llm = FakeLLM([])
    correct(catalog, [case(1, "الكرادة"), case(2, "الكراده"), case(3, "مكان مجهول"), case(4, "بغداد الكرادة"),
                      case(5, "مكان مجهول اخر"), case(6, "الكرادة", "الكرادة قرب الجامع")], "FUHOOD", llm)
    assert llm.calls == [[2, 3, 4, 5]]
    assert llm.hints == {2: "الكرادة", 4: "الكرادة"}


def test_strong_spelling_match_outranks_a_different_ai_pick(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "الناصرية",
                 "addressDetails": "", "stateCode": "DHI", "status": "AI_MATCH", "reason": "meaning"}]
    row = one(catalog, "شطره", "FUHOOD", "DHI", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.status) == ("الشطرة", "NORMALIZED_MATCH")
    assert "AI suggested الناصرية" in row.reason


def test_ai_decides_a_center_kept_for_review(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "طويريج",
                 "addressDetails": "", "stateCode": "KRB", "status": "AI_MATCH", "reason": "typo of طويريج"}]
    row = one(catalog, "كربلاء طوريج", "FUHOOD", "KRB", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.status, row.confidence) == ("طويريج", "AI_MATCH", 0.85)


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
])
def test_district_after_governorate_center(catalog, state, district, expected, details):
    row = one(catalog, district, "FUHOOD", state)
    assert (row.correctDistrict, row.addressDetails) == (expected, details)
    assert row.confidence >= 0.93


@pytest.mark.parametrize("state, district, center", [
    ("KRB", "كربلاء طوريج", "كربلاء"),        # typo: the AI confirms طويريج
    ("BGD", "بغداد الرضوانيه", "بغداد"),      # الرحمانية is only a fuzzy look-alike
    ("KRB", "كربلاء حي الامن", "كربلاء"),     # حي الامين is a different place
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
        return [{"excelSequence": 1, "originalDistrict": cases[0].district, "correctDistrict": "الشطرة",
                 "addressDetails": "", "stateCode": "DHI", "status": "AI_MATCH", "reason": "same"}]
    row = one(catalog, "شطره", "FUHOOD", "DHI", llm=FakeLLM(answer))
    assert (row.correctDistrict, row.confidence) == ("الشطرة", 0.97)


def test_ai_unresolved_falls_back_to_the_suggestion(catalog):
    def answer(cases):
        return [{"excelSequence": 1, "status": "UNRESOLVED"}]
    row = one(catalog, "شطره", "FUHOOD", "DHI", llm=FakeLLM(answer))
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
    ({"originalDistrict": "  مكان   مجهول "}, ("الكرادة", "AI_MATCH")),
    ({"reason": None}, ("الكرادة", "AI_MATCH")),
])
def test_valid_llm_answers_are_accepted(catalog, fields, expected):
    row = one(catalog, "مكان مجهول", "FUHOOD", "BGD", llm=FakeLLM(_answer(**fields)))
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


def test_llm_unresolved_keeps_input(catalog):
    row = one(catalog, "مكان مجهول", "FUHOOD", "BGD", "قرب الجامع", llm=FakeLLM(_answer(status="UNRESOLVED")))
    assert (row.status, row.errorCode, row.addressDetails) == ("UNRESOLVED", None, "قرب الجامع")


def test_partial_llm_answer_only_fails_missing_cases(catalog):
    def build(cases):
        return _answer()(cases[:1]) + [{"excelSequence": 999, "status": "AI_MATCH"}, "junk"]
    rows = correct(catalog, [case(1, "مكان مجهول"), case(2, "مكان مجهول اخر")], "FUHOOD", FakeLLM(build))
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
