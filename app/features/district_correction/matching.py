"""Deterministic exact, spelling, prefix and conservative fuzzy matching."""

import re
from collections import Counter
from difflib import SequenceMatcher
from functools import lru_cache

from .models import CaseRequest, CaseResponse
from .normalization import matching_key, normalize

_WORD = re.compile(r"\S+")
_FUZZY_ACCEPT = 0.89
_FUZZY_MARGIN = 0.08
_FUZZY_PREFILTER = _FUZZY_ACCEPT - _FUZZY_MARGIN


@lru_cache(maxsize=50000)
def _letter_counts(key: str) -> Counter:
    return Counter(key)


def _strip_prefix(text: str, district: str) -> str | None:
    """Remove one leading district only, preserving all other address text."""
    tokens = list(_WORD.finditer(text))
    count = len(district.split())
    if len(tokens) < count or not count:
        return None
    prefix = text[:tokens[count - 1].end()]
    if normalize(prefix) != normalize(district) and matching_key(prefix) != matching_key(district):
        return None
    return text[tokens[count - 1].end():].lstrip(" ،,؛;:-—\t\n")


def _response(case: CaseRequest, district: str, details: str, confidence: float,
              status: str, reason: str) -> CaseResponse:
    return CaseResponse(excelSequence=case.excelSequence, originalDistrict=case.district,
                        correctDistrict=district, addressDetails=details,
                        confidence=confidence, status=status, reason=reason,
                        stateCode=case.stateCode.strip().upper())


def unresolved(case: CaseRequest, reason: str = "No reliable catalog match; original district kept.",
               error: str | None = None) -> CaseResponse:
    result = _response(case, case.district, case.address, 0.2, "UNRESOLVED", reason)
    result.errorCode = error
    return result


def match_case(case: CaseRequest, allowed: list[dict]) -> CaseResponse:
    """Never return a name outside the supplied company and state catalog."""
    names = [entry["name"] for entry in allowed]
    raw = case.district.strip()
    if not raw:
        return unresolved(case, "District is empty.")

    if raw in names:
        details = _strip_prefix(case.address, raw)
        if details is not None and details != case.address:
            return _response(case, raw, details, 1.0, "SPLIT_ADDRESS", "Duplicate district prefix removed from address.")
        return _response(case, raw, case.address, 1.0, "EXACT_MATCH", "Exact company and state catalog match.")

    for key_function in (normalize, matching_key):
        matches = [name for name in names if key_function(name) == key_function(raw)]
        if len(matches) == 1:
            name = matches[0]
            details = _strip_prefix(case.address, name)
            return _response(case, name, case.address if details is None else details,
                             0.97 if key_function is normalize else 0.95,
                             "NORMALIZED_MATCH", "Unique spelling-normalized catalog match.")

    prefixes = [(name, remainder) for name in names if (remainder := _strip_prefix(raw, name)) is not None and remainder]
    if prefixes:
        prefixes.sort(key=lambda item: len(item[0]), reverse=True)
        longest = len(prefixes[0][0])
        if sum(len(name) == longest for name, _ in prefixes) == 1:
            name, remainder = prefixes[0]
            address_tail = _strip_prefix(case.address, name)
            details = remainder
            if case.address.strip():
                details += " " + (case.address if address_tail is None else address_tail)
            return _response(case, name, details.strip(), 0.94, "SPLIT_ADDRESS",
                             "Catalog district extracted from start of district field.")

    target = matching_key(raw)
    target_counts = Counter(target)
    scores = []
    for name in names:
        key = matching_key(name)
        total = len(target) + len(key)
        if not key or 2 * min(len(target), len(key)) / total < _FUZZY_PREFILTER:
            continue
        # Same value as SequenceMatcher.quick_ratio(), without building a matcher per name.
        if 2 * sum((target_counts & _letter_counts(key)).values()) / total >= _FUZZY_PREFILTER:
            scores.append((SequenceMatcher(None, target, key).ratio(), name))
    scores.sort(reverse=True)
    if scores:
        best_score, best_name = scores[0]
        runner = scores[1][0] if len(scores) > 1 else 0.0
        if best_score >= _FUZZY_ACCEPT and best_score - runner >= _FUZZY_MARGIN:
            details = _strip_prefix(case.address, best_name)
            return _response(case, best_name, case.address if details is None else details,
                             round(min(0.93, 0.85 + (best_score - 0.89) * 0.5), 2),
                             "FUZZY_MATCH", "Unique high-similarity catalog match.")
    return unresolved(case)


def candidate_names(text: str, allowed: list[dict], limit: int = 20) -> list[str]:
    key = matching_key(text)
    scores = sorted(((SequenceMatcher(None, key, matching_key(item["name"])).ratio(), item["name"])
                     for item in allowed), reverse=True)
    return [name for _, name in scores[:limit]]
