"""Import all supplied company workbooks into the SQLite catalog."""

import argparse
import json
from pathlib import Path

from app.features.district_correction.catalog import import_catalog
from app.config import settings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=settings.district_source_dir)
    parser.add_argument("--database", type=Path, default=settings.district_database_path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = import_catalog(args.source, args.database)
    body = json.dumps(report, ensure_ascii=False, indent=2)
    summary = {
        "states": report["states"], "total_districts": report["total_districts"],
        "companies": {name: {key: values[key] for key in ("districts", "duplicate_rows", "invalid_rows")}
                      for name, values in report["companies"].items()},
        "invalid_rows": len(report["invalid_rows"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(body + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
