"""The reranker trained in training/train_reranker.py.

The model scores the closest catalog names for a case. In decide mode it settles the rows
it agrees on with the rules and the rows the rules could not place, leaving the LLM only
the disagreements; in shadow mode it runs beside the LLM and only reports its pick. Every
pick is logged for comparison with the final answers.
"""

import json
import logging
import threading
from pathlib import Path

from .matching import candidate_names, without_governorate

logger = logging.getLogger(__name__)


class DistrictReranker:
    def __init__(self, path: Path):
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        path = Path(path)
        meta = json.loads((path / "district_reranker.json").read_text(encoding="utf-8"))
        self.name = path.name
        self.threshold = float(meta["threshold"])
        self.candidates = int(meta.get("candidates", 20))
        self._torch = torch
        self._device = "cuda" if torch.cuda.is_available() else "cpu"
        self._tokenizer = AutoTokenizer.from_pretrained(path)
        self._model = AutoModelForSequenceClassification.from_pretrained(path).to(self._device).eval()
        # One request at a time on the model; concurrent requests queue here, not on the GPU.
        self._lock = threading.Lock()

    def pick(self, district: str, address: str, allowed: list[dict], state_names=(),
             rules_pick: str | None = None) -> tuple[str | None, float | None]:
        """(catalog name or None, its score) among the closest catalog names."""
        text = " ".join(part for part in (district, address) if part)
        names = candidate_names(text, allowed, limit=self.candidates)
        if rules_pick and rules_pick not in names:
            names.append(rules_pick)
        if not names:
            return None, None
        query = without_governorate(text, state_names) or text
        with self._lock, self._torch.no_grad():
            batch = self._tokenizer([query] * len(names), names, padding=True, truncation=True, max_length=96,
                                    return_tensors="pt").to(self._device)
            scores = self._model(**batch).logits.squeeze(-1).float().cpu().tolist()
        best = max(range(len(names)), key=scores.__getitem__)
        return (names[best] if scores[best] >= self.threshold else None), round(scores[best], 4)


def load_reranker(path: Path | None) -> DistrictReranker | None:
    """The reranker at `path`, or None when unset, missing, or its libraries are absent."""
    if not path:
        return None
    try:
        model = DistrictReranker(Path(path))
    except Exception:
        logger.exception("district_reranker_unavailable")
        return None
    logger.info("district_reranker_loaded: %s on %s", path, model._device)
    return model


class ShadowLog:
    """Appends one JSON line per shadowed case, for later comparison with gold labels."""

    def __init__(self, path: Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()

    def write(self, records: list[dict]) -> None:
        if not self.path or not records:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock, open(self.path, "a", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            logger.exception("district_reranker_shadow_log_failed")
