"""Short-word typos, dropped vs. stray letters, and AI picks spelled further than the rules'."""

import asyncio

import pytest

from app.features.district_correction import matching
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.models import CaseRequest, CorrectionRequest
from app.features.district_correction.normalization import sound_key, word_key


def edits(typed, catalog):
    return matching._word_edits(sound_key(word_key(typed)), sound_key(word_key(catalog)))


@pytest.mark.parametrize("typed, catalog", [
    ("الرب", "العرب"),    # dropped letter in a 3-letter word
    ("السحن", "الحسن"),   # neighbours swapped
    ("الصن", "النص"),     # neighbours swapped in a 2-letter word
])
def test_short_words_allow_a_dropped_letter_or_a_swap(typed, catalog):
    assert edits(typed, catalog) is not None


@pytest.mark.parametrize("typed, catalog", [
    ("حسب", "حسن"),     # replaced letter: another real word
    ("نصر", "نص"),      # extra letter in a short word: another real word
])
def test_short_words_still_reject_other_words(typed, catalog):
    assert edits(typed, catalog) is None


def test_a_dropped_letter_is_closer_than_a_stray_extra_letter():
    dropped = edits("الحيرية", "الحيدرية")
    stray = edits("الحيرية", "الحيرة")
    assert dropped is not None and stray is not None and dropped < stray


def test_a_doubled_letter_costs_the_same_as_a_dropped_one():
    assert edits("الكرراده", "الكرادة") == edits("الكراد", "الكرادة")


@pytest.mark.parametrize("text, name", [
    ("بنات السحن - قرب مدرسة النور", "بنات الحسن"),
    ("خان الصن - قرب مدرسة النور", "خان النص"),
    ("صبخة الرب-شارع المطاعم", "صبخة العرب"),
])
def test_short_word_typos_do_not_contradict_the_name(text, name):
    assert not matching.contradicts(text, name)


def test_a_different_last_word_still_contradicts():
    assert matching.contradicts("الدورة ابو دشير", "دورة أبو طيارة")


def test_written_edits_prefers_the_dropped_letter_reading():
    text = "الحيرية-قرب مدرسة النور"
    assert matching.written_edits(text, "الحيدرية") < matching.written_edits(text, "الحيرة")
    assert matching.written_edits(text, "حي الجهاد") is None


class TwoNames:
    states = {"NJF": {"name_ar": "النجف"}}
    by_company = {"X": {"NJF": [{"name": "الحيدرية"}, {"name": "الحيرة"}]}}

    def districts(self, company, code):
        return self.by_company[company][code]


def test_ai_pick_spelled_further_than_the_rules_keeps_the_rules_answer():
    class LLM:
        configured = True

        async def resolve(self, company, code, cases, names, hints=None):
            return [{"excelSequence": 1, "correctDistrict": "الحيرة", "status": "AI_MATCH"}]

    request = CorrectionRequest(companyName="X", cases=[
        CaseRequest(excelSequence=1, stateCode="NJF", district="الحيرية-قرب مدرسة النور")])
    row = asyncio.run(CorrectionService(TwoNames(), LLM()).correct(request))[0].cases[0]

    assert row.correctDistrict == "الحيدرية"
    assert "spelled further" in row.reason


def test_ai_pick_that_agrees_with_the_rules_is_accepted():
    class LLM:
        configured = True

        async def resolve(self, company, code, cases, names, hints=None):
            return [{"excelSequence": 1, "correctDistrict": "الحيدرية", "status": "AI_MATCH"}]

    request = CorrectionRequest(companyName="X", cases=[
        CaseRequest(excelSequence=1, stateCode="NJF", district="الحيرية")])
    row = asyncio.run(CorrectionService(TwoNames(), LLM()).correct(request))[0].cases[0]

    assert row.correctDistrict == "الحيدرية" and row.status == "AI_MATCH"
