"""Compare district benchmark outputs against the company's official XLSX catalog.

This measures catalog validity and a strict exact-input subset; full semantic accuracy
requires reviewed expected labels, which the source benchmark does not include.
"""

import argparse
import json
import tempfile
from collections import Counter
from pathlib import Path

from app.config import settings
from app.features.district_correction.catalog import Catalog, import_catalog
from app.features.district_correction.normalization import normalize


STATE_ALIASES = {
    "الديوانيه": "QAD", "الديوانية": "QAD", "ديوانيه": "QAD", "ديوانية": "QAD",
    "ناصريه": "DHI", "الناصرية": "DHI", "ناصرية": "DHI", "ذي قار": "DHI",
    "الموصل": "NIN", "موصل": "NIN", "نينوى": "NIN",
    "كوت": "WST", "واسط": "WST", "العمارة": "MYS", "عمارة": "MYS", "ميسان": "MYS",
    "السماوة": "MTH", "سماوة": "MTH", "المثنى": "MTH",
}


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def state_for(case, catalog):
    name = case.get("stateName", "").strip()
    alias = next((code for label, code in STATE_ALIASES.items()
                  if normalize(label) == normalize(name)), None)
    named_code = alias or catalog.state_code_for(name)
    # The benchmark contains known-bad courier codes. A recognized governorate name
    # is a better source for judging catalog membership than that bad code.
    return named_code or catalog.state_code_for(case.get("stateCode", ""))


def evaluate_run(cases, outputs, catalog):
    mapped = 0
    answered = 0
    valid = 0
    exact_labeled = 0
    exact_correct = 0
    statuses = Counter()
    invalid_predictions = []
    no_reference_state = []
    for case in cases:
        seq = case["excelSequence"]
        output = outputs[seq]
        code = state_for(case, catalog)
        statuses[output["status"]] += 1
        if not code:
            no_reference_state.append(seq)
            continue
        mapped += 1
        official = catalog.districts("ALZAEEM", code)
        names = {item["name"] for item in official}
        prediction = output["correctDistrict"]
        if output["status"] != "UNRESOLVED":
            answered += 1
            if prediction in names:
                valid += 1
            else:
                invalid_predictions.append({"excelSequence": seq, "referenceStateCode": code,
                                            "prediction": prediction, "status": output["status"]})
        # Exact input-to-catalog matches provide a small, genuinely labeled subset.
        exact_names = {name for name in names if normalize(case.get("district", "")) == normalize(name)}
        if len(exact_names) == 1:
            exact_labeled += 1
            if prediction in exact_names:
                exact_correct += 1
    return {
        "cases": len(cases), "reference_state_mapped": mapped,
        "answered_with_mapped_state": answered, "answered_prediction_in_official_catalog": valid,
        "catalog_validity_rate": round(valid / answered, 4) if answered else None,
        "invalid_catalog_predictions": invalid_predictions,
        "strict_exact_input_labeled_cases": exact_labeled,
        "strict_exact_input_correct": exact_correct,
        "strict_exact_input_accuracy": round(exact_correct / exact_labeled, 4) if exact_labeled else None,
        "statuses": dict(statuses), "state_unmapped_sequences": no_reference_state,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("district-correction-all-cases.json"))
    parser.add_argument("--thinking", type=Path, default=Path("benchmark-results/district-local/results.json"))
    parser.add_argument("--no-thinking", type=Path,
                        default=Path("benchmark-results/district-local-no-thinking/results.json"))
    parser.add_argument("--output", type=Path,
                        default=Path("benchmark-results/district-accuracy-comparison.json"))
    parser.add_argument("--review", type=Path,
                        default=Path("benchmark-results/district-changed-cases-review.json"))
    args = parser.parse_args()

    source = read_json(args.input)
    thinking = read_json(args.thinking)
    no_thinking = read_json(args.no_thinking)
    cases = source["cases"]
    by_run = [{row["excelSequence"]: row for row in report["cases"]}
              for report in (thinking, no_thinking)]
    expected_sequences = [case["excelSequence"] for case in cases]
    if any(set(rows) != set(expected_sequences) for rows in by_run):
        raise ValueError("Input and run outputs do not have the same excelSequence values")

    with tempfile.TemporaryDirectory() as directory:
        db_path = Path(directory) / "catalog.sqlite3"
        import_catalog(settings.district_source_dir, db_path)
        catalog = Catalog(db_path)
        per_run = [evaluate_run(cases, rows, catalog) for rows in by_run]

    changed = []
    same_district = 0
    for case in cases:
        seq = case["excelSequence"]
        first, second = by_run[0][seq], by_run[1][seq]
        if first["correctDistrict"] == second["correctDistrict"]:
            same_district += 1
        else:
            code = state_for(case, catalog)
            official_names = ({item["name"] for item in catalog.districts("ALZAEEM", code)}
                              if code else set())
            changed.append({
                "excelSequence": seq, "stateName": case.get("stateName", ""),
                "inputStateCode": case["stateCode"],
                "referenceStateCode": state_for(case, catalog),
                "district": case["district"], "address": case.get("address", ""),
                "thinkingPredictionInOfficialCatalog": first["correctDistrict"] in official_names,
                "noThinkingPredictionInOfficialCatalog": second["correctDistrict"] in official_names,
                "thinking": {key: first.get(key) for key in ("correctDistrict", "addressDetails", "status", "errorCode")},
                "noThinking": {key: second.get(key) for key in ("correctDistrict", "addressDetails", "status", "errorCode")},
            })

    report = {
        "input": str(args.input), "reference": str(settings.district_source_dir),
        "reference_company": "ALZAEEM", "reference_kind": "official allowed-district catalog",
        "full_semantic_accuracy_available": False,
        "limitation": "The input has no expected district labels. Catalog membership is validity, not semantic correctness.",
        "thinking_run": evaluate_run(cases, by_run[0], catalog),
        "no_thinking_run": evaluate_run(cases, by_run[1], catalog),
        "comparison": {"cases": len(cases), "same_district_prediction": same_district,
                       "different_district_prediction": len(changed),
                       "same_district_rate": round(same_district / len(cases), 4)},
        "changed_case_review_file": str(args.review),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.review.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.review.write_text(json.dumps(changed, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
