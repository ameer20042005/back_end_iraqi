"""SQLite import and cached, company-scoped catalog access."""

import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from .normalization import normalize
from .xlsx_reader import records

CITY_HEADERS = {
    "id": ("District ID", "district_id", "id"),
    "name": ("City Name", "district_name", "name"),
    "state_code": ("Governorate Code", "state_code", "governorate_code"),
}
STATE_HEADERS = {
    "code": ("Code", "state_code", "governorate_code"),
    "name_en": ("Global Name (EN)", "english_name", "name_en"),
    "name_ar": ("Arabic Name", "arabic_name", "name_ar"),
}

SCHEMA = """
CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE);
CREATE TABLE states (code TEXT PRIMARY KEY, name_en TEXT NOT NULL, name_ar TEXT NOT NULL);
CREATE TABLE company_districts (
    id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies(id),
    state_code TEXT NOT NULL REFERENCES states(code), district_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL, source_district_id TEXT,
    UNIQUE(company_id, state_code, district_name)
);
CREATE INDEX idx_district_company_state ON company_districts(company_id, state_code);
CREATE INDEX idx_district_normalized ON company_districts(company_id, state_code, normalized_name);
"""


def _id_text(value: str) -> str:
    return value[:-2] if value.endswith(".0") and value[:-2].isdigit() else value


def import_catalog(source_dir: Path, database_path: Path) -> dict:
    """Inspect all company workbooks and atomically replace the catalog."""
    source_dir = Path(source_dir)
    try:
        report_source = source_dir.resolve().relative_to(Path(__file__).resolve().parents[3]).as_posix()
    except ValueError:
        report_source = str(source_dir)
    pairs = sorted((path.parent.name.upper(), path, path.parent / "governorates.xlsx")
                   for path in source_dir.rglob("cities.xlsx"))
    if not pairs:
        raise ValueError(f"No cities.xlsx files found under {source_dir}")
    if len({company for company, _, _ in pairs}) != len(pairs):
        raise ValueError("Company folder names must be unique")

    states = {}
    company_states = {}
    report = {"source": report_source, "companies": {}, "invalid_rows": [], "state_conflicts": []}
    for company, _, governorates in pairs:
        if not governorates.exists():
            raise ValueError(f"Missing governorates.xlsx for {company}")
        local = {}
        for sheet, row_number, row in records(governorates, STATE_HEADERS):
            code = row["code"].upper()
            if not code or not row["name_en"] or not row["name_ar"]:
                report["invalid_rows"].append({"company": company, "file": governorates.name,
                                                "sheet": sheet, "row": row_number, "reason": "incomplete state"})
                continue
            current = (row["name_en"], row["name_ar"])
            if code in states and states[code] != current:
                report["state_conflicts"].append({"company": company, "code": code,
                                                  "existing": states[code], "incoming": current})
            else:
                states[code] = current
            local[code] = current
        company_states[company] = local
    if report["state_conflicts"]:
        raise ValueError(f"Conflicting governorate definitions: {report['state_conflicts'][:3]}")
    if not states:
        raise ValueError("No governorate definitions found")

    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = database_path.with_name(database_path.name + ".tmp")
    if temporary.exists():
        temporary.unlink()
    try:
        with closing(sqlite3.connect(temporary)) as db:
            db.executescript(SCHEMA)
            db.executemany("INSERT INTO states(code, name_en, name_ar) VALUES (?, ?, ?)",
                           [(code, *names) for code, names in sorted(states.items())])
            for company, cities, _ in pairs:
                company_id = db.execute("INSERT INTO companies(name) VALUES (?) RETURNING id", (company,)).fetchone()[0]
                seen = set()
                counts = Counter()
                duplicates = 0
                invalid = 0
                for sheet, row_number, row in records(cities, CITY_HEADERS):
                    name = row["name"].strip()
                    code = row["state_code"].upper()
                    if not name or code not in states:
                        invalid += 1
                        report["invalid_rows"].append({"company": company, "file": cities.name,
                                                        "sheet": sheet, "row": row_number,
                                                        "reason": "missing name or unknown state",
                                                        "state_code": code, "district_name": name,
                                                        "source_district_id": _id_text(row["id"])})
                        continue
                    key = (code, name)
                    if key in seen:
                        duplicates += 1
                        continue
                    seen.add(key)
                    counts[code] += 1
                    db.execute("""INSERT INTO company_districts
                               (company_id, state_code, district_name, normalized_name, source_district_id)
                               VALUES (?, ?, ?, ?, ?)""",
                               (company_id, code, name, normalize(name), _id_text(row["id"])))
                report["companies"][company] = {
                    "states_in_governorates_file": len(company_states[company]),
                    "shared_governorates_used": not bool(company_states[company]),
                    "districts": sum(counts.values()), "districts_by_state": dict(sorted(counts.items())),
                    "duplicate_rows": duplicates, "invalid_rows": invalid,
                }
            db.commit()
        os.replace(temporary, database_path)
    finally:
        if temporary.exists():
            temporary.unlink()
    report["states"] = len(states)
    report["total_districts"] = sum(item["districts"] for item in report["companies"].values())
    return report


class Catalog:
    """Immutable in-memory snapshot, scoped by company and state."""

    def __init__(self, database_path: Path, excluded_companies: frozenset = frozenset()):
        self.by_company = {}
        self.states = {}
        with closing(sqlite3.connect(database_path)) as db:
            self.states = {code: {"name_en": en, "name_ar": ar}
                           for code, en, ar in db.execute("SELECT code, name_en, name_ar FROM states")}
            for company, code, name, source_id in db.execute("""
                SELECT c.name, d.state_code, d.district_name, d.source_district_id
                FROM company_districts d JOIN companies c ON c.id = d.company_id
                ORDER BY c.name, d.state_code, d.district_name
            """):
                if company in excluded_companies:
                    continue
                self.by_company.setdefault(company, {}).setdefault(code, []).append(
                    {"name": name, "source_id": source_id})

    def companies(self) -> list[str]:
        return sorted(self.by_company)

    def districts(self, company: str, state_code: str) -> list[dict]:
        return self.by_company.get(company.upper(), {}).get(state_code.upper(), [])

    def state_code_for(self, value: str) -> str | None:
        key = normalize(value)
        for code, names in self.states.items():
            if key in (normalize(code), normalize(names["name_en"]), normalize(names["name_ar"])):
                return code
        return None
