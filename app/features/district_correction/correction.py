"""Stateless request orchestration for catalog-safe district correction.

Deterministic matching handles exact, normalized, typo and address-split forms.
The LLM is used for ambiguous cases; small requests may also use it as a verifier,
while large Excel uploads avoid redundant cloud calls for strong local matches.
"""

import asyncio
from collections import Counter, defaultdict

from starlette.concurrency import run_in_threadpool

from .catalog import Catalog
from .llm import LLMClient, LLMError
from .matching import _strip_prefix, candidate_names, match_case, unresolved
from .models import CaseRequest, CaseResponse, CorrectionRequest, CorrectionResponse
from .normalization import normalize, phrase_key

_CHUNK = 20
_CANDIDATES_PER_CASE = 30
_FULL_LIST_LIMIT = 150
_LARGE_REQUEST = 100

# Legacy/source-sheet governorate codes seen in imported Excel files.  The
# catalog uses the canonical codes from governorates.xlsx; these aliases are
# only applied when the raw code is unknown, so a real code/name conflict is
# still rejected instead of silently guessing.
_LEGACY_STATE_CODES = {
    "AMA": "MYS",  # عمارة -> ميسان
    "DWN": "QAD",  # ديوانية -> قادسية
    "KOT": "WST",  # كوت -> واسط
    "MOS": "NIN",  # موصل -> نينوى
    "NAS": "DHI",  # ناصرية -> ذي قار
    "SAM": "MTH",  # سماوة -> المثنى
}


class CorrectionService:
    def __init__(self, catalog: Catalog, llm: LLMClient, max_llm_cases: int = 1000,
                 llm_concurrency: int = 4):
        self.catalog = catalog
        self.llm = llm
        self.max_llm_cases = max_llm_cases
        self.llm_concurrency = max(1, llm_concurrency)

    async def correct(self, request: CorrectionRequest) -> tuple[CorrectionResponse, dict]:
        company = request.companyName.strip().upper()
        if company not in self.catalog.by_company:
            raise ValueError("UNKNOWN_COMPANY")
        results, llm_groups = await run_in_threadpool(self._match_all, company, request.cases)

        sent_to_llm = 0
        jobs = []
        for code, cases in llm_groups.items():
            allowed = self.catalog.districts(company, code)
            full_names = {item["name"] for item in allowed}
            for start in range(0, len(cases), _CHUNK):
                chunk = cases[start:start + _CHUNK]
                room = max(self.max_llm_cases - sent_to_llm, 0)
                for case in chunk[room:]:
                    results[case.excelSequence] = self._fallback(
                        results[case.excelSequence], case, "LLM case limit reached for this request.",
                        "LLM_LIMIT_EXCEEDED")
                chunk = chunk[:room]
                if not chunk:
                    continue
                suggestions = {case.excelSequence: results[case.excelSequence] for case in chunk}
                sent_to_llm += len(chunk)
                jobs.append((code, chunk, allowed, full_names, suggestions))

        # Reserve the case budget before scheduling, so completion order cannot
        # change which rows receive the AI check. Only bounded batches are active.
        semaphore = asyncio.Semaphore(self.llm_concurrency)

        async def resolve_batch(job):
            code, chunk, allowed, full_names, suggestions = job
            async with semaphore:
                candidates = await run_in_threadpool(self._candidates, chunk, allowed, suggestions)
                await self._apply_llm(company, code, chunk, candidates, full_names, suggestions, results)

        await asyncio.gather(*(resolve_batch(job) for job in jobs))

        ordered = [results[case.excelSequence] for case in request.cases]
        counts = Counter(result.status for result in ordered)
        metrics = {"company": company, "states": sorted({case.stateCode.strip().upper() for case in request.cases}),
                   "cases": len(ordered), "exact": counts["EXACT_MATCH"],
                   "fuzzy": counts["FUZZY_MATCH"], "ai": counts["AI_MATCH"], "sent_to_llm": sent_to_llm,
                   "unresolved": counts["UNRESOLVED"]}
        return CorrectionResponse(companyName=company, cases=ordered), metrics

    def _match_all(self, company: str, cases: list[CaseRequest]):
        results: dict[int, CaseResponse] = {}
        llm_groups: dict[str, list[CaseRequest]] = defaultdict(list)
        matched = {}
        state_lists = {}
        large_request = len(cases) > _LARGE_REQUEST
        for case in cases:
            code = case.stateCode.strip().upper()
            named = self.catalog.state_code_for(case.stateName) if case.stateName.strip() else None
            # Accept old source-sheet codes when the canonical catalog code is
            # unambiguous.  Keep rejecting a known code paired with a different
            # known governorate name: that is a genuine data conflict.
            if code not in self.catalog.states:
                alias = _LEGACY_STATE_CODES.get(code)
                if named is not None:
                    code = named
                elif alias in self.catalog.states:
                    code = alias
            if code not in self.catalog.states or (named is not None and named != code and
                                                   case.stateCode.strip().upper() in self.catalog.states):
                results[case.excelSequence] = unresolved(case, "Unknown or inconsistent state.", "UNKNOWN_STATE")
                continue
            if code not in state_lists:
                state_lists[code] = self.catalog.districts(company, code)
            allowed = state_lists[code]
            if not allowed:
                results[case.excelSequence] = unresolved(case, "No districts for this company and state.",
                                                          "EMPTY_DISTRICT_CATALOG")
                continue
            state = self.catalog.states.get(code, {})
            effective_case = case if case.stateCode.strip().upper() == code else case.model_copy(update={"stateCode": code})
            key = (code, case.district, case.address)
            if key in matched:
                result = matched[key].model_copy(update={"excelSequence": case.excelSequence})
            else:
                result = match_case(effective_case, allowed, (state.get("name_ar"), state.get("name_en")))
                if effective_case is not case:
                    result = result.model_copy(update={"originalDistrict": case.district, "stateCode": code})
                matched[key] = result
            results[case.excelSequence] = result
            # Small requests retain the full AI verification behavior.  For a
            # large Excel upload, deterministic normalized/split matches are
            # already catalog-validated; send only unresolved/fuzzy cases to
            # the cloud.  This removes most latency and token usage without
            # weakening the fallback result.
            literal = result.status != "UNRESOLVED" and result.correctDistrict == case.district.strip()
            needs_ai = result.status in ("UNRESOLVED", "FUZZY_MATCH") if large_request else not literal
            if needs_ai and case.district.strip() and self.llm.configured:
                llm_groups[code].append(case)
        return results, llm_groups

    @staticmethod
    def _candidates(chunk: list[CaseRequest], allowed: list[dict], suggestions: dict) -> list[str]:
        """Catalog names shown to the LLM: the whole list when short, else the closest per case."""
        if len(allowed) <= _FULL_LIST_LIMIT:
            return sorted(item["name"] for item in allowed)
        names = set()
        seen_texts = set()
        for case in chunk:
            text = " ".join(filter(None, (case.district, case.address)))
            if text not in seen_texts:
                names.update(candidate_names(text, allowed, limit=_CANDIDATES_PER_CASE))
                seen_texts.add(text)
            suggestion = suggestions.get(case.excelSequence)
            if suggestion is not None and suggestion.status != "UNRESOLVED":
                names.add(suggestion.correctDistrict)
        return sorted(names)

    @staticmethod
    def _catalog_name(value, candidates: list[str]) -> str | None:
        """The LLM's choice as an exact catalog name, tolerating spelling-only differences."""
        if not isinstance(value, str):
            return None
        if value in candidates:
            return value
        for key_function in (normalize, phrase_key):
            matches = [name for name in candidates if key_function(name) == key_function(value)]
            if len(matches) == 1:
                return matches[0]
        return None

    @staticmethod
    def _fallback(suggestion: CaseResponse, case: CaseRequest, why: str, error: str | None) -> CaseResponse:
        """Without a usable AI answer, keep the spelling match if there is one."""
        if suggestion.status != "UNRESOLVED":
            return suggestion.model_copy(update={"reason": f"{suggestion.reason} AI check unavailable: {why}"})
        return unresolved(case, why, error)

    async def _apply_llm(self, company, code, chunk, candidates, full_names, suggestions, results) -> None:
        hints = {sequence: result.correctDistrict for sequence, result in suggestions.items()
                 if result.status != "UNRESOLVED"}
        try:
            answers = await self.llm.resolve(company, code, chunk, candidates, hints)
            if not isinstance(answers, list):
                raise LLMError("LLM_INVALID_RESPONSE")
        except LLMError as exc:
            for case in chunk:
                results[case.excelSequence] = self._fallback(suggestions[case.excelSequence], case,
                                                             str(exc), str(exc))
            return
        by_sequence = {}
        duplicates = set()
        for item in answers:
            if isinstance(item, dict) and type(item.get("excelSequence")) is int:
                if item["excelSequence"] in by_sequence:
                    duplicates.add(item["excelSequence"])
                by_sequence.setdefault(item["excelSequence"], item)
        allowed = [name for name in candidates if name in full_names]
        for case in chunk:
            suggestion = suggestions[case.excelSequence]
            item = by_sequence.get(case.excelSequence)
            if case.excelSequence in duplicates:
                results[case.excelSequence] = self._fallback(
                    suggestion, case, "LLM returned multiple answers for this case.", "LLM_INVALID_RESPONSE")
                continue
            if item is None:
                results[case.excelSequence] = self._fallback(
                    suggestion, case, "LLM returned no answer for this case.", "LLM_INVALID_RESPONSE")
                continue
            status = str(item.get("status") or "").strip().upper()
            if status == "UNRESOLVED":
                results[case.excelSequence] = self._fallback(
                    suggestion, case, "LLM found no reliable catalog district.", None)
                continue
            name = self._catalog_name(item.get("correctDistrict"), allowed)
            original = item.get("originalDistrict")
            state_code = item.get("stateCode")
            suggested_details = item.get("addressDetails")
            suggested_reason = item.get("reason")
            if (status not in ("AI_MATCH", "SPLIT_ADDRESS") or name is None or
                    (original is not None and (not isinstance(original, str) or
                                               normalize(original) != normalize(case.district))) or
                    (state_code is not None and (not isinstance(state_code, str) or
                                                 state_code.strip().upper() != code)) or
                    (suggested_details is not None and not isinstance(suggested_details, str)) or
                    (status == "SPLIT_ADDRESS" and suggested_details is None) or
                    (suggested_reason is not None and not isinstance(suggested_reason, str))):
                results[case.excelSequence] = self._fallback(
                    suggestion, case, "LLM result failed catalog validation.", "LLM_INVALID_RESPONSE")
                continue
            remainder = _strip_prefix(case.district, name)
            address = _strip_prefix(case.address, name)
            details = case.address if address is None else address
            if remainder:
                details = (remainder + " " + details).strip()
            if suggested_details is not None:
                # An empty model field must not erase the customer's address.
                # The deterministic splitter is stronger when both methods agree.
                supplied = suggested_details.strip()
                if supplied:
                    details = supplied
                elif suggestion.status != "UNRESOLVED" and suggestion.correctDistrict == name:
                    details = suggestion.addressDetails
            # Two independent methods agreeing on the same Excel name is the strongest signal.
            agreed = suggestion.status != "UNRESOLVED" and suggestion.correctDistrict == name
            confidence = 0.97 if agreed else 0.9 if status == "SPLIT_ADDRESS" else 0.85
            results[case.excelSequence] = CaseResponse(
                excelSequence=case.excelSequence, originalDistrict=case.district,
                correctDistrict=name, addressDetails=details, confidence=confidence, status=status,
                reason=suggested_reason or "AI selection validated against the company Excel catalog.",
                stateCode=code,
            )
