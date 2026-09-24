"""Stateless request orchestration and strict post-LLM validation."""

from collections import Counter, defaultdict

from starlette.concurrency import run_in_threadpool

from .catalog import Catalog
from .llm import LLMClient, LLMError
from .matching import _strip_prefix, candidate_names, match_case, unresolved
from .models import CaseRequest, CaseResponse, CorrectionRequest, CorrectionResponse


class CorrectionService:
    def __init__(self, catalog: Catalog, llm: LLMClient, max_llm_cases: int = 100):
        self.catalog = catalog
        self.llm = llm
        self.max_llm_cases = max_llm_cases

    async def correct(self, request: CorrectionRequest) -> tuple[CorrectionResponse, dict]:
        company = request.companyName.strip().upper()
        if company not in self.catalog.by_company:
            raise ValueError("UNKNOWN_COMPANY")
        results, unresolved_groups = await run_in_threadpool(self._match_all, company, request.cases)

        sent_to_llm = 0
        for code, cases in unresolved_groups.items():
            allowed = self.catalog.districts(company, code)
            full_names = {item["name"] for item in allowed}
            for start in range(0, len(cases), 20):
                chunk = cases[start:start + 20]
                room = self.max_llm_cases - sent_to_llm
                for case in chunk[max(room, 0):]:
                    results[case.excelSequence] = unresolved(
                        case, "LLM case limit reached for this request.", "LLM_LIMIT_EXCEEDED")
                chunk = chunk[:max(room, 0)]
                if not chunk:
                    continue
                candidates = await run_in_threadpool(self._candidates, chunk, allowed)
                sent_to_llm += len(chunk)
                await self._apply_llm(company, code, chunk, candidates, full_names, results)

        ordered = [results[case.excelSequence] for case in request.cases]
        counts = Counter(result.status for result in ordered)
        metrics = {"company": company, "states": sorted({case.stateCode.strip().upper() for case in request.cases}),
                   "cases": len(ordered), "exact": counts["EXACT_MATCH"],
                   "fuzzy": counts["FUZZY_MATCH"], "sent_to_llm": sent_to_llm,
                   "unresolved": counts["UNRESOLVED"]}
        return CorrectionResponse(companyName=company, cases=ordered), metrics

    def _match_all(self, company: str, cases: list[CaseRequest]):
        results: dict[int, CaseResponse] = {}
        unresolved_groups: dict[str, list[CaseRequest]] = defaultdict(list)
        for case in cases:
            code = case.stateCode.strip().upper()
            if code not in self.catalog.states or (case.stateName and self.catalog.state_code_for(case.stateName) != code):
                results[case.excelSequence] = unresolved(case, "Unknown or inconsistent state.", "UNKNOWN_STATE")
                continue
            allowed = self.catalog.districts(company, code)
            if not allowed:
                results[case.excelSequence] = unresolved(case, "No districts for this company and state.",
                                                          "EMPTY_DISTRICT_CATALOG")
                continue
            result = match_case(case, allowed)
            results[case.excelSequence] = result
            if result.status == "UNRESOLVED" and case.district.strip() and self.llm.configured:
                unresolved_groups[code].append(case)
        return results, unresolved_groups

    @staticmethod
    def _candidates(chunk: list[CaseRequest], allowed: list[dict]) -> list[str]:
        return sorted({name for case in chunk for name in candidate_names(case.district, allowed)})

    async def _apply_llm(self, company, code, chunk, candidates, full_names, results) -> None:
        try:
            suggestions = await self.llm.resolve(company, code, chunk, candidates)
            expected = {case.excelSequence for case in chunk}
            if (len(suggestions) != len(chunk) or
                    any(not isinstance(item, dict) or type(item.get("excelSequence")) is not int
                        for item in suggestions) or
                    {item["excelSequence"] for item in suggestions} != expected):
                raise LLMError("LLM_INVALID_RESPONSE")
            by_sequence = {item["excelSequence"]: item for item in suggestions}
        except LLMError as exc:
            for case in chunk:
                results[case.excelSequence] = unresolved(case, str(exc), str(exc))
            return
        for case in chunk:
            item = by_sequence[case.excelSequence]
            name = item.get("correctDistrict")
            if item.get("status") == "UNRESOLVED":
                continue
            if (item.get("status") != "AI_MATCH" or
                    item.get("originalDistrict") != case.district or
                    not isinstance(item.get("stateCode"), str) or
                    item["stateCode"].upper() != code or
                    not isinstance(name, str) or name not in full_names or name not in candidates):
                results[case.excelSequence] = unresolved(case, "LLM result failed catalog validation.",
                                                          "LLM_INVALID_RESPONSE")
                continue
            remainder = _strip_prefix(case.district, name)
            address = _strip_prefix(case.address, name)
            details = case.address if address is None else address
            if remainder:
                details = (remainder + " " + details).strip()
            results[case.excelSequence] = CaseResponse(
                excelSequence=case.excelSequence, originalDistrict=case.district,
                correctDistrict=name, addressDetails=details, confidence=0.78,
                status="AI_MATCH", reason="LLM selection validated against company and state catalog.",
                stateCode=code,
            )
