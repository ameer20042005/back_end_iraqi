"""Search-only Arabic normalization. Catalog names remain untouched."""

import re
import unicodedata
from functools import lru_cache

_MARKS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]")
_PUNCT = re.compile(r"[،؛,;:!؟?./\\|()\[\]{}\-_]+")
_SPACE = re.compile(r"\s+")
_LETTERS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ؤ": "و", "ئ": "ي", "ی": "ي", "ک": "ك"})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)


@lru_cache(maxsize=50000)
def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = _MARKS.sub("", value).replace("ـ", "")
    value = value.translate(_LETTERS).translate(_DIGITS)
    value = _PUNCT.sub(" ", value)
    return _SPACE.sub(" ", value).strip().casefold()


@lru_cache(maxsize=50000)
def matching_key(value: str) -> str:
    """Secondary spelling signal; never used to rewrite stored names."""
    value = normalize(value).replace("ى", "ي").replace("ة", "ه")
    if value.startswith("ال") and len(value) > 4:
        value = value[2:]
    return value
