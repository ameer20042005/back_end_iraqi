"""Check the labeler's JSONL answers against the catalog and merge them into data/gold.jsonl.

A name differing only in spelling from one catalog name is replaced by that name; any other
unknown name, unknown id or bad field rejects the line. Low-confidence lines go to
data/review.jsonl for a person to confirm; corrected lines saved as data/labels/*reviewed*.jsonl
with "confidence": "high" then count as gold and override the model's answer.

    python training/validate_labels.py
"""

import json
from pathlib import Path

from common import DATA, ERROR_TYPES, catalog, read_jsonl, write_jsonl

from app.features.district_correction.normalization import phrase_key


def _batch_index() -> dict[str, dict]:
    """id -> {companyName, stateCode, row} from every exported batch."""
    out = {}
    for path in sorted((DATA / "batches").glob("*.json")):
        batch = json.loads(path.read_text(encoding="utf-8"))
        for row in batch["rows"]:
            out[row["id"]] = {"companyName": batch["companyName"], "stateCode": batch["stateCode"], "row": row}
    return out


def _catalog_name(value, names: set[str]) -> str | None:
    if value in names:
        return value
    matches = [name for name in names if phrase_key(name) == phrase_key(value)]
    return matches[0] if len(matches) == 1 else None


def main() -> None:
    index = catalog()
    batches = _batch_index()
    gold, review, problems = {}, {}, []
    # A person's corrections (files named *reviewed*.jsonl) override the model's lines.
    for path in sorted((DATA / "labels").glob("*.jsonl"), key=lambda path: ("reviewed" in path.name, path.name)):
        for number, item in enumerate(read_jsonl(path), start=1):
            where = f"{path.name}:{number}"
            info = batches.get(item.get("id"))
            if info is None:
                problems.append(f"{where} unknown id {item.get('id')!r}")
                continue
            names = {entry["name"] for entry in index.districts(info["companyName"], info["stateCode"])}
            value = item.get("goldDistrict")
            if value is not None:
                value = _catalog_name(value, names) if isinstance(value, str) else None
                if value is None:
                    problems.append(f"{where} {item['id']}: goldDistrict {item.get('goldDistrict')!r} not in catalog")
                    continue
            acceptable = []
            for name in item.get("acceptable") or []:
                fixed = _catalog_name(name, names) if isinstance(name, str) else None
                if fixed is None:
                    problems.append(f"{where} {item['id']}: acceptable {name!r} not in catalog (dropped)")
                elif fixed != value:
                    acceptable.append(fixed)
            if item.get("errorType") is not None and item["errorType"] not in ERROR_TYPES:
                problems.append(f"{where} {item['id']}: errorType {item.get('errorType')!r} (kept, recomputed later)")
            record = {"id": item["id"], "companyName": info["companyName"], "stateCode": info["stateCode"],
                      "district": info["row"]["district"], "address": info["row"]["address"],
                      "goldDistrict": value, "acceptable": acceptable,
                      "addressDetails": item.get("addressDetails", ""), "note": item.get("note", ""),
                      "labeler": item.get("labeler", path.stem)}
            target = review if item.get("confidence") == "low" else gold
            (gold if target is review else review).pop(item["id"], None)
            target[item["id"]] = record
    write_jsonl(DATA / "gold.jsonl", gold.values())
    write_jsonl(DATA / "review.jsonl", review.values())
    print(f"gold: {len(gold)} rows -> {DATA / 'gold.jsonl'}")
    print(f"needs review (confidence low): {len(review)} rows -> {DATA / 'review.jsonl'}")
    print(f"problems: {len(problems)}")
    for line in problems[:50]:
        print("  " + line)


if __name__ == "__main__":
    main()
