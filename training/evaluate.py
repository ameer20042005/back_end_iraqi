"""Measure the service against gold labels: accuracy, error types, and the wrong rows.

    python training/evaluate.py                       # rules only (no LLM, offline)
    python training/evaluate.py --api-url http://127.0.0.1:8000 --api-key ...   # full service
    python training/evaluate.py --model training/models/reranker   # rules + trained reranker
    python training/evaluate.py --heldout --api-url ... --model ...    # only rows the model never saw
"""

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from common import DATA, Row, api_predictions, catalog, error_type, load_gold, read_jsonl, rules_predictions, \
    state_names, write_jsonl

from app.features.district_correction.matching import is_center


def gold_rows(path: Path) -> list[Row]:
    return [Row(id=item["id"], companyName=item["companyName"], stateCode=item["stateCode"], stateName="",
                district=item["district"], address=item.get("address", "")) for item in read_jsonl(path)]


def report(rows: list[Row], predictions: dict, gold: dict, index, title: str) -> list[dict]:
    errors, by_company = [], defaultdict(Counter)
    types = Counter()
    for row in rows:
        predicted = predictions[row.id]["district"]
        general = predicted is not None and is_center(predicted, state_names(index, row.stateCode))
        kind = error_type(gold[row.id], predicted, general)
        types[kind] += 1
        by_company[row.companyName][kind] += 1
        if kind != "CORRECT":
            errors.append({"id": row.id, "errorType": kind, "district": row.district, "predicted": predicted,
                           "gold": gold[row.id].goldDistrict, "acceptable": gold[row.id].acceptable,
                           "note": gold[row.id].note})
    total = len(rows)
    print(f"\n== {title}: {total} gold rows")
    print(f"accuracy: {types['CORRECT'] / total:.1%}  ({types['CORRECT']}/{total})")
    for kind, count in types.most_common():
        if kind != "CORRECT":
            print(f"  {kind:15} {count:4}  {count / total:.1%}")
    sent = [value["sentToLlm"] for value in predictions.values() if value.get("sentToLlm") is not None]
    if sent:
        print(f"rows the rules leave to the LLM: {sum(sent)}/{len(sent)}")
    for company, counts in sorted(by_company.items()):
        count = sum(counts.values())
        print(f"  {company:8} {counts['CORRECT'] / count:.1%} of {count}")
    return errors


def _with_model(rows, base: dict, reranker, index, fallback: bool) -> dict:
    """The model's pick for every row, or (fallback) only where the base has none or just a center."""
    out = {}
    for row in rows:
        pick = base[row.id]["district"]
        weak = pick is None or is_center(pick, state_names(index, row.stateCode))
        out[row.id] = {"district": reranker.choose(row, index, pick) if weak or not fallback else pick,
                       "sentToLlm": None}
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold", type=Path, default=DATA / "gold.jsonl")
    parser.add_argument("--api-url")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--model", type=Path, help="a reranker trained by train_reranker.py")
    parser.add_argument("--show", type=int, default=30, help="wrong rows printed")
    parser.add_argument("--heldout", action="store_true",
                        help="only the rows train_reranker.py kept out of training (same split)")
    args = parser.parse_args()

    index = catalog()
    gold = load_gold(args.gold)
    rows = gold_rows(args.gold)
    if args.heldout:
        from train_reranker import _split
        rows = _split(rows, 0.2, 13)[1]
    if args.api_url:
        predictions, title = api_predictions(rows, args.api_url, args.api_key), "full service (API)"
        write_jsonl(DATA / "api_predictions.jsonl", ({"id": key, **value} for key, value in predictions.items()))
    else:
        predictions, title = rules_predictions(rows, index), "rules only"
    errors = report(rows, predictions, gold, index, title)
    if args.model:
        from train_reranker import Reranker
        reranker = Reranker(args.model)
        report(rows, _with_model(rows, predictions, reranker, index, fallback=False), gold, index,
               f"reranker alone ({args.model.name})")
        errors = report(rows, _with_model(rows, predictions, reranker, index, fallback=True), gold, index,
                        f"{title} + reranker where it has no district or only a center")
    write_jsonl(DATA / "errors.jsonl", errors)
    print(f"\nwrong rows -> {DATA / 'errors.jsonl'}")
    for item in errors[:args.show]:
        print(f"  [{item['errorType']}] {item['district'][:60]}  -> {item['predicted']}  (gold: {item['gold']})")


if __name__ == "__main__":
    main()
