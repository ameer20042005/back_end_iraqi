"""Stateless request orchestration for catalog-safe district correction.

Deterministic matching handles exact, normalized, typo and address-split forms.
The LLM is used for ambiguous cases. With a trained reranker in decide mode the order is
rules, then the model, then the LLM only where the rules and the model disagree.
"""

import asyncio
import logging
from datetime import datetime, timezone
from collections import Counter, defaultdict

from starlette.concurrency import run_in_threadpool

from .aliases import AUTO, REVIEWER, AliasStore
from .catalog import Catalog
from .llm import LLMClient, LLMError
from .matching import (CENTER_REVIEW, CERTAIN_REASONS, _strip_prefix, adds_unwritten, candidate_names,
                       clean_details, contradicts, is_center, match_case, memory_key, named_as_facility,
                       remembered_details, text_support, unresolved, without_district, without_governorate)
from .models import (CaseRequest, CaseResponse, CorrectionRequest, CorrectionResponse, FeedbackRejection,
                     FeedbackRequest, FeedbackResponse)
from .normalization import normalize, phrase_key
from .reranker import DistrictReranker, ShadowLog
from .semantic import EmbeddingError, SemanticIndex

logger = logging.getLogger(__name__)
_MEMORY_REASON = "Confirmed correction remembered for this company and governorate."
_LEARNED_REASON = "Learned from an earlier answer two independent methods agreed on."
_REMEMBERED = {REVIEWER: (_MEMORY_REASON, 0.98), AUTO: (_LEARNED_REASON, 0.95)}
_SEMANTIC_AGREES = " Semantic search agrees."
_MODEL_AGREES = " The trained model agrees."
_MODEL_REASON = "Picked by the trained model among the closest catalog names (score {:.2f})."
_MODEL_UNSURE = " The trained model found no reliable district."
_SIMILAR_PER_CASE = 10

_CHUNK = 20
_CANDIDATES_PER_CASE = 30
_FULL_LIST_LIMIT = 150
# A new AI pick must be written in the text (typos allowed); see text_support.
_AI_SUPPORT = 0.78

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
                 llm_concurrency: int = 4, aliases: AliasStore | None = None,
                 semantic: SemanticIndex | None = None, auto_learn: bool = False,
                 reranker: DistrictReranker | None = None, shadow_log: ShadowLog | None = None,
                 reranker_decides: bool = False):
        self.catalog = catalog
        self.llm = llm
        self.max_llm_cases = max_llm_cases
        self.llm_concurrency = max(1, llm_concurrency)
        self.aliases = aliases
        self.semantic = semantic
        self.auto_learn = auto_learn and aliases is not None
        self.reranker = reranker
        self.shadow_log = shadow_log
        self.reranker_decides = reranker_decides

    def _state_names(self, code: str) -> tuple:
        state = self.catalog.states.get(code, {})
        return state.get("name_ar"), state.get("name_en")

    def resolve_state(self, state_code: str, state_name: str) -> str | None:
        """Catalog governorate code, or None when unknown or contradicted by the name."""
        raw = state_code.strip().upper()
        code = raw
        named = self.catalog.state_code_for(state_name) if state_name.strip() else None
        # Accept old source-sheet codes when the canonical catalog code is
        # unambiguous.  Keep rejecting a known code paired with a different
        # known governorate name: that is a genuine data conflict.
        if code not in self.catalog.states:
            alias = _LEGACY_STATE_CODES.get(code)
            if named is not None:
                code = named
            elif alias in self.catalog.states:
                code = alias
        if code not in self.catalog.states or (named is not None and named != code and raw in self.catalog.states):
            return None
        return code

    def remember(self, request: FeedbackRequest, forget: bool = False) -> FeedbackResponse:
        """Save (or with `forget`, remove) reviewer-confirmed districts for later requests."""
        company = request.companyName.strip().upper()
        if company not in self.catalog.by_company:
            raise ValueError("UNKNOWN_COMPANY")
        rows, rejected = [], []
        for index, item in enumerate(request.corrections):
            code = self.resolve_state(item.stateCode, item.stateName)
            if code is None:
                rejected.append(FeedbackRejection(index=index, code="UNKNOWN_STATE"))
                continue
            key = memory_key(item.district, self._state_names(code))
            if not key:
                rejected.append(FeedbackRejection(index=index, code="EMPTY_TEXT"))
                continue
            if forget:
                rows.append((company, code, key))
                continue
            name = (item.correctDistrict or "").strip()
            if not name:
                rejected.append(FeedbackRejection(index=index, code="MISSING_DISTRICT"))
            elif name not in {entry["name"] for entry in self.catalog.districts(company, code)}:
                rejected.append(FeedbackRejection(index=index, code="UNKNOWN_DISTRICT"))
            else:
                rows.append((company, code, key, name, item.district.strip()))
        if forget:
            return FeedbackResponse(companyName=company, removed=self.aliases.forget(rows), rejected=rejected)
        return FeedbackResponse(companyName=company, saved=self.aliases.learn(rows), rejected=rejected)

    async def correct(self, request: CorrectionRequest) -> tuple[CorrectionResponse, dict]:
        company = request.companyName.strip().upper()
        if company not in self.catalog.by_company:
            raise ValueError("UNKNOWN_COMPANY")
        results, llm_groups = await run_in_threadpool(self._match_all, company, request.cases)
        # Answers two independent methods agreed on, saved for the next request.
        learned: dict[int, tuple] = {}
        similar, semantic_agreed = await self._semantic_check(company, llm_groups, results, learned)

        shadow = self._shadow_targets(request.cases, llm_groups, results) if self.reranker else {}
        rules_picks = {sequence: None if results[sequence].status == "UNRESOLVED" else results[sequence].correctDistrict
                       for sequence in shadow}
        deciding = bool(shadow) and self.reranker_decides
        model_picks, model_settled = {}, 0
        if deciding:
            # Rules, then the trained model, then the LLM as referee: the model settles the
            # rows it agrees on with the rules and the rows the rules could not place; only
            # the rows where the two disagree reach the LLM (best on held-out gold, training/).
            model_picks = await run_in_threadpool(self._model_picks, company, shadow, rules_picks)
            model_settled = self._model_decide(shadow, rules_picks, model_picks, results, llm_groups, learned)

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
                candidates = await run_in_threadpool(self._candidates, chunk, allowed, suggestions, similar)
                await self._apply_llm(company, code, chunk, candidates, full_names, suggestions, results, similar,
                                      learned)

        # In shadow mode the trained reranker reads the same uncertain cases in a worker thread
        # while the LLM batches are awaited: in parallel, adding no latency, and never deciding.
        model_task = (asyncio.ensure_future(run_in_threadpool(self._model_picks, company, shadow, rules_picks))
                      if shadow and not deciding else None)
        await asyncio.gather(*(resolve_batch(job) for job in jobs))
        if model_task:
            model_picks = await model_task
        model_agreed = self._record_shadow(company, shadow, rules_picks, model_picks, results)
        saved = await run_in_threadpool(self._learn, company, learned) if self.auto_learn and learned else 0

        ordered = [results[case.excelSequence] for case in request.cases]
        counts = Counter(result.status for result in ordered)
        metrics = {"company": company, "states": sorted({case.stateCode.strip().upper() for case in request.cases}),
                   "cases": len(ordered), "exact": counts["EXACT_MATCH"],
                   "fuzzy": counts["FUZZY_MATCH"], "ai": counts["AI_MATCH"], "sent_to_llm": sent_to_llm,
                   "remembered": sum(result.reason in (_MEMORY_REASON, _LEARNED_REASON) for result in ordered),
                   "learned": saved,
                   "semantic_agreed": semantic_agreed, "model_cases": len(model_picks),
                   "model_settled": model_settled,
                   "model_agreed": model_agreed, "unresolved": counts["UNRESOLVED"]}
        return CorrectionResponse(companyName=company, cases=ordered), metrics

    def _shadow_targets(self, cases: list[CaseRequest], llm_groups: dict, results: dict) -> dict:
        """{excelSequence: (state code, case)} for the cases the rules did not settle."""
        targets = {case.excelSequence: (code, case) for code, group in llm_groups.items() for case in group}
        for case in cases:
            result = results[case.excelSequence]
            if (case.excelSequence not in targets and result.status == "UNRESOLVED" and result.errorCode is None
                    and result.stateCode in self.catalog.states and case.district.strip()):
                targets[case.excelSequence] = (result.stateCode, case)
        return targets

    def _model_picks(self, company: str, targets: dict, rules_picks: dict) -> dict:
        """{excelSequence: (name or None, score)}; empty when the model fails."""
        picks = {}
        try:
            for sequence, (code, case) in targets.items():
                picks[sequence] = self.reranker.pick(case.district, case.address,
                                                     self.catalog.districts(company, code),
                                                     self._state_names(code), rules_picks[sequence])
        except Exception:
            logger.exception("district_reranker_failed")
            return {}
        return picks

    def _model_decide(self, targets: dict, rules_picks: dict, model_picks: dict, results: dict,
                      llm_groups: dict, learned: dict) -> int:
        """Settle what the model can and leave in llm_groups only the rows the LLM referees.

        Rules and model agree -> the rules' answer, confirmed. The rules found a district and
        the model another (or none) -> the LLM decides. The rules found nothing or only the
        center -> the model's pick when it clears its threshold, else the rules' result stays:
        an LLM guess there added more false districts than it found (training/evaluate.py).
        Returns the number of rows settled without the LLM.
        """
        settled = set()
        for sequence, (name, score) in model_picks.items():
            code, case = targets[sequence]
            result, rule = results[sequence], rules_picks[sequence]
            state_names = self._state_names(code)
            center = rule is not None and is_center(rule, state_names)
            if name is not None and name == rule:
                update = {"reason": result.reason + _MODEL_AGREES}
                if not center:
                    update["confidence"] = max(result.confidence, 0.95)
                    learned[sequence] = (code, case, rule)
                results[sequence] = result.model_copy(update=update)
            elif rule is not None and not center:
                continue
            elif name is not None:
                details, moved = remembered_details(case, name, state_names)
                results[sequence] = CaseResponse(
                    excelSequence=case.excelSequence, originalDistrict=case.district, correctDistrict=name,
                    addressDetails=details, confidence=0.9, status="SPLIT_ADDRESS" if moved else "AI_MATCH",
                    reason=_MODEL_REASON.format(score), stateCode=code)
            else:
                results[sequence] = result.model_copy(update={"reason": result.reason + _MODEL_UNSURE})
            settled.add(sequence)
        for code in list(llm_groups):
            llm_groups[code] = [case for case in llm_groups[code] if case.excelSequence not in settled]
            if not llm_groups[code]:
                del llm_groups[code]
        return len(settled)

    def _record_shadow(self, company: str, targets: dict, rules_picks: dict, model_picks: dict,
                       results: dict) -> int:
        """Show the model's pick beside the answer and log both; returns how often they agree."""
        agreed, records = 0, []
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for sequence, (name, score) in model_picks.items():
            result = results[sequence]
            final = None if result.status == "UNRESOLVED" else result.correctDistrict
            agreed += name == final
            results[sequence] = result.model_copy(update={"modelDistrict": name})
            code, case = targets[sequence]
            records.append({"time": now, "company": company, "stateCode": code, "excelSequence": sequence,
                            "district": case.district, "address": case.address, "rules": rules_picks[sequence],
                            "final": final, "finalStatus": result.status, "model": name, "modelScore": score,
                            "reranker": self.reranker.name})
        if self.shadow_log is not None:
            self.shadow_log.write(records)
        return agreed

    async def _semantic_check(self, company: str, llm_groups: dict, results: dict,
                              learned: dict) -> tuple[dict, int]:
        """Rank catalog names by meaning for the cases bound for the LLM.

        A rule result that is also the clear semantic first choice is settled here; the
        others go on to the LLM with the closest names as extra candidates. Returns
        (similar names per excelSequence, cases settled).
        """
        if self.semantic is None or not self.semantic.configured or not llm_groups:
            return {}, 0
        similar, agreed = {}, 0
        try:
            for code, cases in llm_groups.items():
                texts = [without_governorate(case.district, self._state_names(code)) or case.district
                         for case in cases]
                rankings = await self.semantic.rank(company, code, self.catalog.districts(company, code), texts,
                                                    limit=_SIMILAR_PER_CASE)
                kept = []
                for case, ranking in zip(cases, rankings):
                    result = results[case.excelSequence]
                    if (ranking and result.status != "UNRESOLVED" and result.confidence > CENTER_REVIEW
                            and ranking[0][0] == result.correctDistrict
                            and (len(ranking) < 2 or ranking[0][1] - ranking[1][1] >= self.semantic.agree_margin)):
                        results[case.excelSequence] = result.model_copy(
                            update={"reason": result.reason + _SEMANTIC_AGREES})
                        agreed += 1
                        learned[case.excelSequence] = (code, case, result.correctDistrict)
                        continue
                    similar[case.excelSequence] = [name for name, _ in ranking]
                    kept.append(case)
                cases[:] = kept
        except EmbeddingError as exc:
            # Semantic search only assists; without it the request runs as before.
            logger.warning("district_semantic_search_unavailable: %s", exc)
            return {}, 0
        for code in [code for code, cases in llm_groups.items() if not cases]:
            del llm_groups[code]
        return similar, agreed

    def _remembered(self, company: str, code: str, case: CaseRequest, allowed: list[dict]) -> CaseResponse | None:
        """The reviewer-confirmed district for this text, if it is still in the catalog."""
        if self.aliases is None:
            return None
        state_names = self._state_names(code)
        found = self.aliases.get(company, code, memory_key(case.district, state_names))
        if found is None or found[0] not in {entry["name"] for entry in allowed}:
            return None
        name, source = found
        reason, confidence = _REMEMBERED.get(source, _REMEMBERED[AUTO])
        details, moved = remembered_details(case, name, state_names)
        return CaseResponse(excelSequence=case.excelSequence, originalDistrict=case.district,
                            correctDistrict=name, addressDetails=details, confidence=confidence,
                            status="SPLIT_ADDRESS" if moved else "NORMALIZED_MATCH",
                            reason=reason, stateCode=code)

    def _learn(self, company: str, learned: dict) -> int:
        rows = {}
        for code, case, name in learned.values():
            key = memory_key(case.district, self._state_names(code))
            if key:
                rows[(company, code, key)] = (company, code, key, name, case.district.strip())
        return self.aliases.learn(list(rows.values()), source=AUTO)

    def _match_all(self, company: str, cases: list[CaseRequest]):
        results: dict[int, CaseResponse] = {}
        llm_groups: dict[str, list[CaseRequest]] = defaultdict(list)
        matched = {}
        state_lists = {}
        for case in cases:
            code = self.resolve_state(case.stateCode, case.stateName)
            if code is None:
                results[case.excelSequence] = unresolved(case, "Unknown or inconsistent state.", "UNKNOWN_STATE")
                continue
            if code not in state_lists:
                state_lists[code] = self.catalog.districts(company, code)
            allowed = state_lists[code]
            if not allowed:
                results[case.excelSequence] = unresolved(case, "No districts for this company and state.",
                                                          "EMPTY_DISTRICT_CATALOG")
                continue
            effective_case = case if case.stateCode.strip().upper() == code else case.model_copy(update={"stateCode": code})
            key = (code, case.district, case.address)
            if key in matched:
                result = matched[key].model_copy(update={"excelSequence": case.excelSequence})
            else:
                result = (self._remembered(company, code, case, allowed)
                          or match_case(effective_case, allowed, self._state_names(code)))
                if effective_case is not case:
                    result = result.model_copy(update={"originalDistrict": case.district, "stateCode": code})
                matched[key] = result
            results[case.excelSequence] = result
            # The rules settle only what involves no guess (the catalog name up to
            # spelling, or with the governorate/label words around it), and reviewers'
            # confirmed corrections. Splits, typos, centers and unresolved text go to the
            # AI, whatever the request size; the rule result is its hint and the fallback
            # when the AI is unavailable.
            certain = result.status == "EXACT_MATCH" or result.reason in (_MEMORY_REASON, _LEARNED_REASON) or (
                result.status == "NORMALIZED_MATCH" and result.reason in CERTAIN_REASONS)
            literal = result.status != "UNRESOLVED" and result.correctDistrict == case.district.strip()
            needs_ai = not (certain or literal)
            if needs_ai and case.district.strip() and self.llm.configured:
                llm_groups[code].append(case)
        return results, llm_groups

    @staticmethod
    def _candidates(chunk: list[CaseRequest], allowed: list[dict], suggestions: dict,
                    similar: dict | None = None) -> list[str]:
        """Catalog names shown to the LLM: the whole list when short, else the closest per case
        by spelling and, when semantic search runs, by meaning."""
        if len(allowed) <= _FULL_LIST_LIMIT:
            return sorted(item["name"] for item in allowed)
        names = set()
        seen_texts = set()
        for case in chunk:
            text = " ".join(filter(None, (case.district, case.address)))
            if text not in seen_texts:
                names.update(candidate_names(text, allowed, limit=_CANDIDATES_PER_CASE))
                seen_texts.add(text)
            names.update((similar or {}).get(case.excelSequence, ()))
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

    async def _apply_llm(self, company, code, chunk, candidates, full_names, suggestions, results,
                         similar=None, learned=None) -> None:
        hints = {sequence: result.correctDistrict for sequence, result in suggestions.items()
                 if result.status != "UNRESOLVED"}
        # Closest names by meaning, limited to this batch and to names the LLM may choose.
        allowed_names = set(candidates)
        nearby = {case.excelSequence: [name for name in similar[case.excelSequence][:3] if name in allowed_names]
                  for case in chunk if similar and case.excelSequence in similar}
        nearby = {sequence: names for sequence, names in nearby.items() if names}
        try:
            answers = await self.llm.resolve(company, code, chunk, candidates, hints,
                                             **({"similar": nearby} if nearby else {}))
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
            text = f"{case.district} {case.address}"
            if name != suggestion.correctDistrict and text_support(text, name) < _AI_SUPPORT:
                # A small model fills gaps with plausible districts the text never names
                # ("جسر ديالى" -> "الاعظمية"); unresolved is safer than a wrong delivery.
                results[case.excelSequence] = self._fallback(
                    suggestion, case, f"AI pick {name} is not written in the text.", None)
                continue
            if name != suggestion.correctDistrict and contradicts(text, name):
                # Shares the first words with the text but not the rest ("ابو دشير" -> "أبو طيارة").
                results[case.excelSequence] = self._fallback(
                    suggestion, case, f"AI pick {name} differs from the place written in the text.", None)
                continue
            if (name != suggestion.correctDistrict and suggestion.status != "UNRESOLVED"
                    and adds_unwritten(text, name, suggestion.correctDistrict)):
                # A longer catalog name whose extra words are not in the text ("شارع فلسطين"
                # -> "الادريسي / شارع فلسطين").
                results[case.excelSequence] = self._fallback(
                    suggestion, case, f"AI pick {name} adds words the text does not write.", None)
                continue
            if name != suggestion.correctDistrict and named_as_facility(text, name):
                # "مقابل المركز الشرطه" names a police station, not "حي الشرطة".
                results[case.excelSequence] = self._fallback(
                    suggestion, case, f"AI pick {name} is only part of a landmark name in the text.", None)
                continue
            state_names = self._state_names(code)
            if (name != suggestion.correctDistrict and suggestion.status != "UNRESOLVED"
                    and suggestion.confidence >= 0.93 and is_center(name, state_names)
                    and not is_center(suggestion.correctDistrict, state_names)):
                # The center is context; the rules found the district written after it
                # ("السماوة المثنى قضاء الخضر" stays "الخضر", not "السماوة").
                results[case.excelSequence] = suggestion.model_copy(update={
                    "reason": f"{suggestion.reason} AI pick {name} is only the governorate center."})
                continue
            remainder = _strip_prefix(case.district, name)
            address = _strip_prefix(case.address, name)
            details = case.address if address is None else address
            if remainder:
                details = (remainder + " " + details).strip()
            if suggested_details is not None:
                # An empty model field must not erase the customer's address.
                # The deterministic splitter is stronger when both methods agree.
                supplied = without_district(clean_details(suggested_details, state_names), name, state_names)
                if supplied:
                    details = supplied
                elif suggestion.status != "UNRESOLVED" and suggestion.correctDistrict == name:
                    details = suggestion.addressDetails
            # Two independent methods agreeing on the same Excel name is the strongest signal,
            # unless the rules themselves flagged it for review (a center kept on doubt).
            agreed = (suggestion.status != "UNRESOLVED" and suggestion.correctDistrict == name
                      and suggestion.confidence > CENTER_REVIEW)
            confidence = 0.97 if agreed else 0.9 if status == "SPLIT_ADDRESS" else 0.85
            if learned is not None and (agreed or name == ((similar or {}).get(case.excelSequence) or [None])[0]):
                learned[case.excelSequence] = (code, case, name)
            results[case.excelSequence] = CaseResponse(
                excelSequence=case.excelSequence, originalDistrict=case.district,
                correctDistrict=name, addressDetails=details, confidence=confidence, status=status,
                reason=suggested_reason or "AI selection validated against the company Excel catalog.",
                stateCode=code,
            )
