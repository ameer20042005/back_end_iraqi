"""Shared helpers for the district-model workspace: rows, catalog, system predictions.

Everything here reads the service's own catalog and matching code, so gold labels,
evaluations and training data use exactly the names the service can return.
"""

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
DATA = ROOT / "data"
sys.path.insert(0, str(PROJECT))
# Models downloaded by the training scripts stay inside this workspace's venv.
os.environ.setdefault("HF_HOME", str(ROOT / ".venv" / "hf-cache"))

from app.config import settings  # noqa: E402
from app.features.district_correction.catalog import Catalog, import_catalog  # noqa: E402
from app.features.district_correction.models import CaseRequest  # noqa: E402
from app.features.district_correction.xlsx_reader import workbook_sheets  # noqa: E402

# Error types for the system's answer compared with the gold answer.
ERROR_TYPES = ("CORRECT", "WRONG_DISTRICT", "TOO_GENERAL", "MISSED", "FALSE_MATCH")


@dataclass
class Row:
    id: str
    companyName: str
    stateCode: str
    stateName: str
    district: str
    address: str = ""

    def case(self, sequence: int) -> CaseRequest:
        return CaseRequest(excelSequence=sequence, stateCode=self.stateCode, stateName=self.stateName,
                           district=self.district, address=self.address)


@dataclass
class Gold:
    id: str
    goldDistrict: str | None
    acceptable: list[str] = field(default_factory=list)
    note: str = ""

    def accepts(self, name: str | None) -> bool:
        if self.goldDistrict is None:
            return name is None
        return name is not None and name in {self.goldDistrict, *self.acceptable}


def catalog() -> Catalog:
    """A fresh catalog built from the Excel workbooks in assets/address."""
    path = Path(tempfile.mkdtemp()) / "districts.sqlite3"
    import_catalog(settings.district_source_dir, path)
    return Catalog(path)


def _clean_address(value: str, district: str) -> str:
    value = (value or "").strip()
    return "" if value in ("—", "-", district) else value


def rows_from_workbook(path: Path, sheets: set[str] | None = None) -> list[Row]:
    """Rows of a JSON-to-Excel export: sheets named <COMPANY>_<n>."""
    rows = []
    for sheet, values in workbook_sheets(Path(path)):
        if "_" not in sheet or (sheets and sheet not in sheets):
            continue
        company = sheet.rsplit("_", 1)[0].upper()
        for value in values[1:]:
            if not value.get("D") or not value.get("C"):
                continue
            rows.append(Row(id=f"{sheet}#{value['A']}", companyName=company, stateCode=value["C"],
                            stateName=value.get("B", ""), district=value["D"],
                            address=_clean_address(value.get("E", ""), value["D"])))
    return rows


def rows_from_request(path: Path) -> list[Row]:
    """Rows of a saved /v1/district-correction request body."""
    body = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    company = body["companyName"].strip().upper()
    return [Row(id=f"{Path(path).stem}#{case['excelSequence']}", companyName=company,
                stateCode=case["stateCode"], stateName=case.get("stateName", ""),
                district=case.get("district", ""),
                address=_clean_address(case.get("address", ""), case.get("district", "")))
            for case in body["cases"]]


def load_rows(sources: list[str], sheets: set[str] | None = None) -> list[Row]:
    rows = []
    for source in sources:
        path = Path(source)
        rows += rows_from_workbook(path, sheets) if path.suffix.lower() == ".xlsx" else rows_from_request(path)
    return rows


def read_jsonl(path: Path) -> list[dict]:
    lines = Path(path).read_text(encoding="utf-8-sig").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def write_jsonl(path: Path, items) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")


def load_gold(path: Path) -> dict[str, Gold]:
    return {item["id"]: Gold(item["id"], item.get("goldDistrict"), item.get("acceptable") or [], item.get("note", ""))
            for item in read_jsonl(path)}


class _RoutingOnly:
    """Lets the rules report which rows they would send to the LLM, without calling it."""
    configured = True


def rules_predictions(rows: list[Row], index: Catalog) -> dict[str, dict]:
    """The deterministic layer's answer per row: {id: {district, status, sentToLlm}}."""
    from app.features.district_correction.correction import CorrectionService

    service = CorrectionService(index, _RoutingOnly())
    out = {}
    by_company: dict[str, list[Row]] = {}
    for row in rows:
        by_company.setdefault(row.companyName, []).append(row)
    for company, items in by_company.items():
        cases = [row.case(number) for number, row in enumerate(items, start=1)]
        results, groups = service._match_all(company, cases)
        to_llm = {case.excelSequence for group in groups.values() for case in group}
        for number, row in enumerate(items, start=1):
            result = results[number]
            out[row.id] = {"district": None if result.status == "UNRESOLVED" else result.correctDistrict,
                           "status": result.status, "sentToLlm": number in to_llm}
    return out


def api_predictions(rows: list[Row], url: str, key: str, chunk: int = 200) -> dict[str, dict]:
    """The full service's answer (rules + LLM + memory) from a running server."""
    import httpx

    out = {}
    by_company: dict[str, list[Row]] = {}
    for row in rows:
        by_company.setdefault(row.companyName, []).append(row)
    with httpx.Client(timeout=900) as client:
        for company, items in by_company.items():
            for start in range(0, len(items), chunk):
                part = items[start:start + chunk]
                body = {"companyName": company,
                        "cases": [row.case(number).model_dump() for number, row in enumerate(part, start=1)]}
                response = client.post(url.rstrip("/") + "/v1/district-correction", json=body,
                                       headers={"X-API-Key": key})
                response.raise_for_status()
                for result in response.json()["cases"]:
                    row = part[result["excelSequence"] - 1]
                    out[row.id] = {"district": None if result["status"] == "UNRESOLVED" else result["correctDistrict"],
                                   "status": result["status"], "sentToLlm": None}
    return out


def error_type(gold: Gold, predicted: str | None, general: bool = False) -> str:
    """`general`: the prediction is the governorate or its center while a district exists."""
    if gold.accepts(predicted):
        return "CORRECT"
    if predicted is None:
        return "MISSED"
    if gold.goldDistrict is None:
        return "FALSE_MATCH"
    return "TOO_GENERAL" if general else "WRONG_DISTRICT"


def state_names(index: Catalog, code: str) -> tuple:
    state = index.states.get(code, {})
    return state.get("name_ar"), state.get("name_en")
