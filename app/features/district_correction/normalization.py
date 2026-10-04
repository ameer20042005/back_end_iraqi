"""Search-only Arabic normalization. Catalog names remain untouched."""

import re
import unicodedata
from functools import lru_cache

_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭ]")
PUNCTUATION = "،؛,;:!؟?./\\|()[]{}-_–—«»\"'`"
_PUNCT = re.compile("[" + re.escape(PUNCTUATION) + "]+")
_SPACE = re.compile(r"\s+")
# Arabic keyboard variants and letters used in Iraqi dialect writing.
_LETTERS = str.maketrans({
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ؤ": "و", "ئ": "ي", "ی": "ي",
    "ک": "ك", "گ": "ك", "چ": "ج", "پ": "ب",
})
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "0123456789" * 2)

# Descriptive words that precede a district name without changing which district
# it is ("مجمع بوابة الكاظمية" = "بوابة الكاظمية"). Stored as word keys.
GENERIC_LABELS = frozenset({"حي", "منطقه", "مجمع", "محله", "جمعيه", "قضا", "ناحيه", "مدينه", "قريه"})
GOVERNORATE_LABEL = "محافظه"


@lru_cache(maxsize=50000)
def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip()
    value = _MARKS.sub("", value).replace("ـ", "")
    value = value.translate(_LETTERS).translate(_DIGITS)
    value = _PUNCT.sub(" ", value)
    return _SPACE.sub(" ", value).strip().casefold()


@lru_cache(maxsize=50000)
def surface_key(value: str) -> str:
    """Letters as written (no letter folding); only marks, tatweel, punctuation and spaces drop."""
    value = unicodedata.normalize("NFKC", value or "")
    value = _MARKS.sub("", value).replace("ـ", "")
    return _SPACE.sub(" ", _PUNCT.sub(" ", value)).strip().casefold()


@lru_cache(maxsize=50000)
def matching_key(value: str) -> str:
    """Secondary spelling signal; never used to rewrite stored names."""
    value = normalize(value).replace("ى", "ي").replace("ة", "ه")
    if value.startswith("ال") and len(value) > 4:
        value = value[2:]
    return value


@lru_cache(maxsize=100000)
def word_key(word: str) -> str:
    """Spelling key of one word: ى/ي, ة/ه, hamza and the article are ignored."""
    value = normalize(word).replace("ى", "ي").replace("ة", "ه").replace("ء", "")
    if value.startswith("ال") and len(value) > 4:
        value = value[2:]
    return value


# Ordinals written as words or digits name the same place ("نور ستي الثانيه" = "نور ستي2").
# Only for exact lookups: the typo and fuzzy tiers keep the letters ("السالثة" -> "الثالثة").
_ORDINALS = {"اول": "1", "اولي": "1", "ثاني": "2", "ثانيه": "2", "ثالث": "3", "ثالثه": "3",
             "رابع": "4", "رابعه": "4", "خامس": "5", "خامسه": "5"}


def ordinal_key(words: list[str]) -> str:
    """Compact key of phrase-key words with ordinal words as digits."""
    return "".join(_ORDINALS.get(word, word) for word in words)


@lru_cache(maxsize=50000)
def phrase_key(value: str) -> str:
    return " ".join(key for key in (word_key(word) for word in normalize(value).split()) if key)


@lru_cache(maxsize=50000)
def compact_key(value: str) -> str:
    """Phrase key without spaces, so "عبد الله" and "عبدالله" meet."""
    return phrase_key(value).replace(" ", "")


def without_label(words: list[str]) -> list[str] | None:
    """Words after one leading descriptive label ("مجمع بوابة الكاظمية" -> "بوابة الكاظمية")."""
    return words[1:] if len(words) > 1 and words[0] in GENERIC_LABELS else None


# Letters Iraqi writers swap because they sound alike (الظباط/الضباط، مسفى/مصفى، المذبانية/المزبانية).
_SOUND_ALIKE = str.maketrans({"ظ": "ض", "ذ": "ز", "ص": "س", "ث": "س", "ط": "ت"})

# Words that start the address part of a field; used to split glued text ("رواندزقرب").
ADDRESS_WORDS = frozenset({
    "قرب", "مقابل", "خلف", "جنب", "امام", "بجانب", "قريب", "شارع", "زقاق", "محله", "دار", "بيت", "عماره",
    "مدرسه", "جامع", "مسجد", "سوق", "فرع", "ساحه", "مستشفي", "الطابق", "بنايه", "مول", "فندق", "حي",
    "منطقه", "مجمع", "طريق", "تقاطع", "نهايه", "بدايه", "داخل", "مدخل",
})


@lru_cache(maxsize=100000)
def sound_key(word: str) -> str:
    """Typo-comparison form of one word: spelling folds plus sound-alike letters."""
    return normalize(word).replace("ى", "ي").replace("ة", "ه").replace("ء", "").translate(_SOUND_ALIKE)


@lru_cache(maxsize=100000)
def loose_key(key: str) -> str:
    """Fuzzy-only key: a final alef reads like a final taa marbuta (عينكاوا/عينكاوة)."""
    return " ".join(word[:-1] + "ه" if len(word) > 1 and word.endswith("ا") else word for word in key.split())
