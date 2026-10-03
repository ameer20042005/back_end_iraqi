"""Run from the repository root: python -m scripts.benchmark_district_correction.

Uses the real catalog, no network, and a fixed-delay fake model to measure batch
orchestration separately from model inference. Times are medians, in milliseconds.
"""

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from app.config import settings
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.matching import candidate_names
from app.features.district_correction.models import CaseRequest, CorrectionRequest


class NoLLM:
    configured = False


class DelayedLLM:
    configured = True

    async def resolve(self, company, code, cases, names, hints=None):
        await asyncio.sleep(0.05)
        return [{"excelSequence": case.excelSequence, "status": "UNRESOLVED"} for case in cases]


def median_ms(action):
    elapsed = []
    for _ in range(3):
        started = time.perf_counter()
        action()
        elapsed.append((time.perf_counter() - started) * 1000)
    return round(statistics.median(elapsed), 2)


def main():
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "districts.sqlite3"
        import_catalog(settings.district_source_dir, path)
        catalog = Catalog(path)
        repeated = CorrectionRequest(companyName="FUHOOD", cases=[
            CaseRequest(excelSequence=i, stateCode="DHI", district="شطره قرب السوق العام")
            for i in range(1000)])
        hard = json.loads((Path(__file__).resolve().parents[1] / "tests/data/district_hard_cases.json")
                          .read_text(encoding="utf-8"))
        requests = [CorrectionRequest(companyName=company, cases=[
            CaseRequest(excelSequence=i, stateCode=item["state"], district=item["district"])
            for i, item in enumerate(hard) if item["company"] == company])
            for company in sorted({item["company"] for item in hard})]
        allowed = catalog.districts("ALZAEEM", "KRB")
        shortlist_inputs = [name["name"].replace(" ", "") + " قرب الجامع" for name in allowed[:20]]
        batches = CorrectionRequest(companyName="FUHOOD", cases=[
            CaseRequest(excelSequence=i, stateCode="DHI", district=f"مكان مجهول {i}")
            for i in range(100)])
        print(json.dumps({
            "repeated_1000_ms": median_ms(lambda: asyncio.run(CorrectionService(catalog, NoLLM()).correct(repeated))),
            "hard_100_ms": median_ms(lambda: [asyncio.run(CorrectionService(catalog, NoLLM()).correct(r))
                                              for r in requests]),
            "shortlist_20_ms": median_ms(lambda: [candidate_names(text, allowed, 30) for text in shortlist_inputs]),
            "five_fake_llm_batches_ms": median_ms(lambda: asyncio.run(
                CorrectionService(catalog, DelayedLLM()).correct(batches))),
            "hard_cases_correct_in_top_30": sum(item["expected"] in candidate_names(
                item["district"], catalog.districts(item["company"], item["state"]), 30) for item in hard),
            "hard_cases_total": len(hard),
        }, indent=2))


if __name__ == "__main__":
    main()
