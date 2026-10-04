"""Train a cross-encoder that picks the district among catalog candidates, from gold labels.

For every gold row the candidates are the catalog names closest to the text (the service's
own candidate_names) plus the rules' answer. The model scores each (text, name) pair; the
best name wins when its score clears a threshold tuned on held-out rows, otherwise the row
stays unresolved. Rows with the same text never sit on both sides of the split.

    python training/train_reranker.py --epochs 3
    python training/evaluate.py --model training/models/reranker
"""

import argparse
import hashlib
import json
import random
from pathlib import Path

from common import ROOT, DATA, Row, catalog, load_gold, read_jsonl, rules_predictions

from app.features.district_correction.correction import CorrectionService
from app.features.district_correction.matching import candidate_names, without_governorate
from app.features.district_correction.normalization import phrase_key

BASE_MODEL = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"  # multilingual, 118M parameters
CANDIDATES = 20


def row_text(row: Row, names) -> str:
    text = " ".join(part for part in (row.district, row.address) if part)
    return without_governorate(text, names) or text


def candidates(row: Row, index, rules_pick: str | None) -> list[str]:
    service = CorrectionService(index, None)
    code = service.resolve_state(row.stateCode, row.stateName) or row.stateCode
    allowed = index.districts(row.companyName, code)
    text = " ".join(part for part in (row.district, row.address) if part)
    names = candidate_names(text, allowed, limit=CANDIDATES)
    if rules_pick and rules_pick not in names:
        names.append(rules_pick)
    return names


def _split(rows: list[Row], holdout: float, seed: int) -> tuple[list[Row], list[Row]]:
    """Group by spelling-normalized text so a repeated address never leaks into the test side."""
    def bucket(row: Row) -> float:
        digest = hashlib.sha1(f"{seed}:{phrase_key(row.district)}".encode()).hexdigest()
        return int(digest[:8], 16) / 0xFFFFFFFF
    train = [row for row in rows if bucket(row) >= holdout]
    test = [row for row in rows if bucket(row) < holdout]
    return train, test


class Reranker:
    def __init__(self, path: Path):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.torch = torch
        meta = json.loads((Path(path) / "district_reranker.json").read_text(encoding="utf-8"))
        self.threshold = meta["threshold"]
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.tokenizer = AutoTokenizer.from_pretrained(path)
        self.model = AutoModelForSequenceClassification.from_pretrained(path).to(self.device).eval()

    def scores(self, text: str, names: list[str]) -> list[float]:
        if not names:
            return []
        with self.torch.no_grad():
            batch = self.tokenizer([text] * len(names), names, padding=True, truncation=True, max_length=96,
                                   return_tensors="pt").to(self.device)
            return self.model(**batch).logits.squeeze(-1).float().cpu().tolist()

    def choose(self, row: Row, index, rules_pick: str | None) -> str | None:
        state = index.states.get(row.stateCode, {})
        names = candidates(row, index, rules_pick)
        values = self.scores(row_text(row, (state.get("name_ar"), state.get("name_en"))), names)
        if not values:
            return None
        best = max(range(len(names)), key=values.__getitem__)
        return names[best] if values[best] >= self.threshold else None


def _examples(rows, gold, index, picks):
    """(text, name, label) pairs: gold and acceptable names are positive, other candidates negative."""
    pairs = []
    for row in rows:
        label = gold[row.id]
        state = index.states.get(row.stateCode, {})
        text = row_text(row, (state.get("name_ar"), state.get("name_en")))
        names = candidates(row, index, picks[row.id]["district"])
        positives = {label.goldDistrict, *label.acceptable} - {None}
        for name in positives - set(names):
            names.append(name)  # training sees the answer even when retrieval missed it
        pairs += [(text, name, 1.0 if name in positives else 0.0) for name in names]
    return pairs


def _accuracy(rows, gold, picks) -> float:
    return sum(gold[row.id].accepts(picks[row.id]) for row in rows) / max(len(rows), 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold", type=Path, default=DATA / "gold.jsonl")
    parser.add_argument("--base", default=BASE_MODEL)
    parser.add_argument("--out", type=Path, default=ROOT / "models" / "reranker")
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=16)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--holdout", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=13)
    args = parser.parse_args()

    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    random.seed(args.seed)
    torch.manual_seed(args.seed)
    index = catalog()
    gold = load_gold(args.gold)
    rows = [Row(id=item["id"], companyName=item["companyName"], stateCode=item["stateCode"], stateName="",
                district=item["district"], address=item.get("address", "")) for item in read_jsonl(args.gold)]
    train, test = _split(rows, args.holdout, args.seed)
    picks = rules_predictions(rows, index)
    pairs = _examples(train, gold, index, picks)
    print(f"gold rows: {len(rows)} (train {len(train)}, test {len(test)}); training pairs: {len(pairs)}")
    if len(rows) < 1000:
        print("note: fewer than 1000 gold rows; this run checks the pipeline, it is not a production model")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    tokenizer = AutoTokenizer.from_pretrained(args.base)
    model = AutoModelForSequenceClassification.from_pretrained(args.base, num_labels=1,
                                                               ignore_mismatched_sizes=True).to(device)
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
    loss_function = torch.nn.BCEWithLogitsLoss()
    model.train()
    for epoch in range(1, args.epochs + 1):
        random.shuffle(pairs)
        total = 0.0
        for start in range(0, len(pairs), args.batch_size):
            batch = pairs[start:start + args.batch_size]
            encoded = tokenizer([text for text, _, _ in batch], [name for _, name, _ in batch], padding=True,
                                truncation=True, max_length=96, return_tensors="pt").to(device)
            labels = torch.tensor([label for _, _, label in batch], device=device)
            loss = loss_function(model(**encoded).logits.squeeze(-1), labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            total += loss.item() * len(batch)
        print(f"epoch {epoch}: loss {total / len(pairs):.4f}")

    args.out.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(args.out)
    tokenizer.save_pretrained(args.out)
    meta = {"base": args.base, "threshold": float("-inf"), "candidates": CANDIDATES,
            "gold_rows": len(rows), "train_rows": len(train), "test_rows": len(test)}
    (args.out / "district_reranker.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    # Threshold: the best accuracy on the training rows (the test rows stay untouched for the report).
    reranker = Reranker(args.out)
    scored = []
    for row in train:
        state = index.states.get(row.stateCode, {})
        names = candidates(row, index, picks[row.id]["district"])
        values = reranker.scores(row_text(row, (state.get("name_ar"), state.get("name_en"))), names)
        best = max(range(len(names)), key=values.__getitem__) if values else None
        scored.append((row, names[best] if best is not None else None, values[best] if best is not None else None))
    thresholds = sorted({score for _, _, score in scored if score is not None}) or [0.0]
    def accuracy_at(limit):
        return sum(gold[row.id].accepts(name if score is not None and score >= limit else None)
                   for row, name, score in scored)
    meta["threshold"] = max([float("-inf")] + thresholds, key=accuracy_at)
    (args.out / "district_reranker.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")

    reranker = Reranker(args.out)
    model_picks = {row.id: reranker.choose(row, index, picks[row.id]["district"]) for row in test}
    rules_picks = {row.id: picks[row.id]["district"] for row in test}
    print(f"threshold: {meta['threshold']:.3f}")
    print(f"held-out rows: {len(test)}")
    print(f"  rules only : {_accuracy(test, gold, rules_picks):.1%}")
    print(f"  reranker   : {_accuracy(test, gold, model_picks):.1%}")
    print(f"model -> {args.out}")


if __name__ == "__main__":
    main()
