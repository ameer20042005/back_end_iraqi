"""Write labeling batches: one company and governorate per file, with its allowed districts.

Each batch is pasted after prompts/labeling_prompt.md into a strong model; its JSONL answer
is saved under data/labels/ and checked by validate_labels.py.

    python training/export_batches.py "JSON_to_Excel (1).xlsx" --sheets FUHOOD_3,FUHOOD_4
    python training/export_batches.py request.json --api-url http://127.0.0.1:8000 --api-key ...
"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from common import DATA, api_predictions, catalog, load_rows, read_jsonl, rules_predictions

from app.features.district_correction.correction import CorrectionService


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sources", nargs="+", help="JSON-to-Excel workbooks or saved request bodies")
    parser.add_argument("--sheets", default="", help="comma-separated sheet names (workbooks only)")
    parser.add_argument("--rows-per-batch", type=int, default=40)
    parser.add_argument("--out", type=Path, default=DATA / "batches")
    parser.add_argument("--api-url", help="score with the running service (rules + LLM) instead of rules only")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--skip-labeled", action="store_true", help="leave out rows already in data/labels/")
    args = parser.parse_args()

    index = catalog()
    rows = load_rows(args.sources, {name for name in args.sheets.split(",") if name} or None)
    if args.skip_labeled:
        done = {item["id"] for path in (DATA / "labels").glob("*.jsonl") for item in read_jsonl(path)}
        rows = [row for row in rows if row.id not in done]
    predictions = (api_predictions(rows, args.api_url, args.api_key) if args.api_url
                   else rules_predictions(rows, index))

    resolver = CorrectionService(index, None)
    groups = defaultdict(list)
    skipped = 0
    for row in rows:
        code = resolver.resolve_state(row.stateCode, row.stateName)
        if code is None or not index.districts(row.companyName, code):
            skipped += 1
            continue
        groups[(row.companyName, code)].append(row)

    args.out.mkdir(parents=True, exist_ok=True)
    written = 0
    for (company, code), items in sorted(groups.items()):
        names = sorted(entry["name"] for entry in index.districts(company, code))
        state = index.states[code]
        for part, start in enumerate(range(0, len(items), args.rows_per_batch), start=1):
            batch = {
                "companyName": company, "stateCode": code, "stateName": state["name_ar"],
                "allowedDistricts": names,
                "rows": [{"id": row.id, "district": row.district, "address": row.address,
                          "systemDistrict": predictions[row.id]["district"]}
                         for row in items[start:start + args.rows_per_batch]],
            }
            path = args.out / f"{company}_{code}_{part:03d}.json"
            path.write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
            written += 1
    print(f"{len(rows)} rows -> {written} batches in {args.out} ({skipped} rows with an unknown state skipped)")


if __name__ == "__main__":
    main()
