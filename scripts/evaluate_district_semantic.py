"""Measure the semantic search layer on an exported shipments workbook before enabling it.

For every row the rules would send to the LLM, the configured embeddings server ranks the
catalog. The report shows how many rows each agreement margin would settle without the
LLM, and lists the rows to review: settled ones and those whose first semantic choice
differs from the rules. Sheets are named <COMPANY>_<n> with the columns excelSequence,
stateName, stateCode, district and address (the JSON-to-Excel export).

    DISTRICT_EMBEDDING_BASE_URL=http://127.0.0.1:8001 DISTRICT_EMBEDDING_MODEL=BAAI/bge-m3 \
    python -m scripts.evaluate_district_semantic "JSON_to_Excel (1).xlsx" --show 40
"""

import argparse
import asyncio
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from app.config import settings
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.matching import CENTER_REVIEW, without_governorate
from app.features.district_correction.models import CaseRequest
from app.features.district_correction.semantic import SemanticIndex
from app.features.district_correction.xlsx_reader import workbook_sheets

MARGINS = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08, 0.1)


class _RoutingOnly:
    """Marks the LLM as available so the rules report which rows it would receive."""
    configured = True


def _cases(path: Path):
    """{company: [(sheet, CaseRequest)]} with a request-unique excelSequence."""
    by_company = defaultdict(list)
    for sheet, rows in workbook_sheets(path):
        if "_" not in sheet or not rows:
            continue
        company = sheet.rsplit("_", 1)[0].upper()
        for row in rows[1:]:
            if not row.get("D") or not row.get("C"):
                continue
            address = row.get("E", "")
            by_company[company].append((sheet, CaseRequest(
                excelSequence=len(by_company[company]) + 1, stateName=row.get("B", ""), stateCode=row["C"],
                district=row["D"], address="" if address in ("—", row["D"]) else address)))
    return by_company


async def _evaluate(path: Path, show: int) -> None:
    database = Path(tempfile.mkdtemp()) / "districts.sqlite3"
    import_catalog(settings.district_source_dir, database)
    catalog = Catalog(database)
    semantic = SemanticIndex(settings)
    if not semantic.configured:
        raise SystemExit("Set DISTRICT_EMBEDDING_BASE_URL and DISTRICT_EMBEDDING_MODEL first.")
    service = CorrectionService(catalog, _RoutingOnly())
    rows, settled = [], defaultdict(int)
    total = sent = 0
    started = time.monotonic()
    for company, items in _cases(path).items():
        if company not in catalog.by_company:
            continue
        sheets = {case.excelSequence: sheet for sheet, case in items}
        results, groups = service._match_all(company, [case for _, case in items])
        total += len(items)
        for code, cases in groups.items():
            sent += len(cases)
            texts = [without_governorate(case.district, service._state_names(code)) or case.district
                     for case in cases]
            rankings = await semantic.rank(company, code, catalog.districts(company, code), texts)
            for case, ranking in zip(cases, rankings):
                rule = results[case.excelSequence]
                margin = ranking[0][1] - ranking[1][1] if len(ranking) > 1 else 1.0
                same = (rule.status != "UNRESOLVED" and rule.confidence > CENTER_REVIEW
                        and ranking[0][0] == rule.correctDistrict)
                for limit in MARGINS:
                    settled[limit] += same and margin >= limit
                rows.append((same, margin, sheets[case.excelSequence], case.district,
                             rule.correctDistrict if rule.status != "UNRESOLVED" else "—", ranking[:3]))
    elapsed = time.monotonic() - started
    print(f"model={settings.district_embedding_model} rows={total} sent_to_llm_by_rules={sent} "
          f"seconds={elapsed:.1f}")
    for limit in MARGINS:
        print(f"  margin>={limit:<5} settles {settled[limit]:5d} of {sent} ({settled[limit] / max(sent, 1):.0%})")
    for title, chosen in (("Settled without the LLM (rules = semantic first choice)", True),
                          ("Semantic first choice differs from the rules", False)):
        print(f"\n{title}:")
        for same, margin, sheet, text, rule, ranking in [row for row in rows if row[0] is chosen][:show]:
            top = " | ".join(f"{name} {score:.2f}" for name, score in ranking)
            print(f"  {sheet}: {text}\n      rules={rule}  margin={margin:.3f}  semantic={top}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("workbook", type=Path)
    parser.add_argument("--show", type=int, default=25, help="rows listed per section")
    arguments = parser.parse_args()
    asyncio.run(_evaluate(arguments.workbook, arguments.show))


if __name__ == "__main__":
    main()
