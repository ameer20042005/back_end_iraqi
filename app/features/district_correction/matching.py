"""Deterministic exact, spelling, label/governorate-aware prefix and conservative fuzzy matching."""

import html
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from functools import lru_cache

from .models import CaseRequest, CaseResponse
from .normalization import (ADDRESS_WORDS, GENERIC_LABELS, GOVERNORATE_LABEL, PUNCTUATION, compact_key,
                            loose_key, matching_key, normalize, ordinal_key, phrase_key, sound_key,
                            surface_key, without_label, word_key)

_TOKEN = re.compile("[^\\s" + re.escape(PUNCTUATION) + "]+")
_NUMBER = re.compile(r"\d+")
_EDGES = " \t\nـ" + PUNCTUATION
_FUZZY_ACCEPT = 0.89
_FUZZY_MARGIN = 0.08
_FUZZY_PREFILTER = _FUZZY_ACCEPT - _FUZZY_MARGIN
_EXTRA_WORDS_ACCEPT = 0.75
_MIN_FUZZY_KEY = 4
AMBIGUOUS = object()
_AMBIGUOUS_REASON = "Several catalog districts match equally; original district kept."
# Governorates and their centers, which people write before the real district
# ("الناصرية الشطرة", "الحلة المسيب"). Not qadha centers such as الفلوجة or
# الزبير: their neighborhoods share names with other cities in the same governorate.
_CENTERS = frozenset(phrase_key(name) for name in (
    "بغداد", "البصرة", "الموصل", "نينوى", "الناصرية", "ذي قار", "الديوانية", "القادسية", "الحلة",
    "بابل", "العمارة", "ميسان", "الكوت", "واسط", "السماوة", "المثنى", "كربلاء", "النجف", "الرمادي",
    "الانبار", "بعقوبة", "ديالى", "تكريت", "صلاح الدين", "كركوك", "اربيل", "السليمانية", "دهوك"))
# Each governorate's own center and other names, by its Arabic catalog name. Only these are
# skipped as context before a district: "القادسية" is a governorate, and a Tikrit neighborhood.
_STATE_CENTERS = {phrase_key(state): frozenset(phrase_key(name) for name in names) for state, names in {
    "بغداد": ("بغداد",), "البصرة": ("البصرة",), "نينوى": ("الموصل", "نينوى"), "ذي قار": ("الناصرية", "ذي قار"),
    "القادسية": ("الديوانية", "القادسية"), "بابل": ("الحلة", "بابل"), "ميسان": ("العمارة", "ميسان"),
    "واسط": ("الكوت", "واسط"), "المثنى": ("السماوة", "المثنى"), "كربلاء": ("كربلاء",), "النجف": ("النجف",),
    "الانبار": ("الرمادي", "الانبار"), "ديالى": ("بعقوبة", "ديالى"), "صلاح الدين": ("تكريت", "صلاح الدين"),
    "كركوك": ("كركوك",), "اربيل": ("اربيل",), "السليمانية": ("السليمانية",), "دهوك": ("دهوك",),
}.items()}
# Honorifics written after a city name ("النجف الاشرف", "كربلاء المقدسة").
_HONORIFICS = frozenset({word_key("الاشرف"), word_key("المقدسة")})
# Baghdad's two banks, written after the governorate as context ("بغداد الرصافة البنوك").
# Never a typo of a district: "الرصافة" is not "الرسالة".
_SIDES = frozenset({word_key("الكرخ"), word_key("الرصافة")})
_INSIDE_CENTER_REASON = "Specific catalog district written after the governorate center."
_TYPO_REASON = "Catalog district matched despite spelling mistakes."
_SPELLING_REASON = "Unique spelling-normalized catalog match."
_DUPLICATE_REASON = "Closest spelling among duplicate catalog entries for one place."
_WORDS_IGNORED_REASON = "Catalog match after ignoring label or governorate words."
# Matches that involve no guess: the text is a catalog name up to spelling, or that name
# with only the governorate or a label word around it. Everything else goes to the AI.
CERTAIN_REASONS = frozenset({_SPELLING_REASON, _DUPLICATE_REASON, _WORDS_IGNORED_REASON})
_PREFIX_REASON = "Only catalog district that begins with the written words."
# Confidence of a center kept while the rest of the text resembles another district:
# below the large-request AI threshold, so the AI checks it.
CENTER_REVIEW = 0.8
_REVIEW = object()


@lru_cache(maxsize=50000)
def _letter_counts(key: str) -> Counter:
    return Counter(key)


@lru_cache(maxsize=100000)
def _numbers(key: str) -> tuple[str, ...]:
    return tuple(_NUMBER.findall(key))


def _ratio(first: str, second: str) -> float:
    return SequenceMatcher(None, first, second).ratio()


@lru_cache(maxsize=50000)
def _similarity(first: str, second: str) -> float:
    """Spelling similarity; numbers must agree ("شارع 20" is never "شارع 40")."""
    if _numbers(first) != _numbers(second):
        return 0.0
    score = _ratio(first, second)
    loose_first, loose_second = loose_key(first), loose_key(second)
    if (loose_first, loose_second) != (first, second):
        score = max(score, _ratio(loose_first, loose_second))
    return score


def _may_be_similar(first: str, second: str) -> bool:
    """Cheap upper bound of the ratio (SequenceMatcher.quick_ratio) before the real comparison."""
    total = len(first) + len(second)
    if not second or 2 * min(len(first), len(second)) / total < _FUZZY_PREFILTER:
        return False
    return 2 * sum((_letter_counts(first) & _letter_counts(second)).values()) / total >= _FUZZY_PREFILTER - 0.05


# Typing mistakes by how often they happen: a dropped or doubled letter is the most
# common, then a stray extra letter, then two neighbouring letters swapped, then one
# letter replaced by another.
_MISSING_OR_EXTRA = 0.8
# An extra letter that does not double its neighbour ("الحيرية" for "الحيرة") is rarer
# than a dropped one ("الحيرية" for "الحيدرية"); at equal letter counts the dropped
# reading wins instead of the two being an unresolvable tie.
_STRAY_EXTRA = 0.85
_SWAPPED = 0.9
_REPLACED = 1.0


def _extra_cost(word: str, position: int) -> float:
    """Cost of `word[position]` being an extra letter: cheaper when it doubles a neighbour."""
    letter = word[position]
    doubled = (position > 0 and word[position - 1] == letter) or (
        position + 1 < len(word) and word[position + 1] == letter)
    return _MISSING_OR_EXTRA if doubled else _STRAY_EXTRA


def _edits(first: str, second: str, limit: float) -> float:
    """Weighted edit distance of typed `first` from catalog `second`; stops above `limit`.

    Missing or doubled letter < stray extra letter < swapped < replaced letter.
    """
    if abs(len(first) - len(second)) * _MISSING_OR_EXTRA > limit:
        return limit + 1
    before, previous = None, [j * _MISSING_OR_EXTRA for j in range(len(second) + 1)]
    for i in range(1, len(first) + 1):
        extra = _extra_cost(first, i - 1)
        current = [previous[0] + extra] + [0.0] * len(second)
        for j in range(1, len(second) + 1):
            current[j] = min(previous[j] + extra, current[j - 1] + _MISSING_OR_EXTRA,
                             previous[j - 1] + (_REPLACED if first[i - 1] != second[j - 1] else 0.0))
            if i > 1 and j > 1 and first[i - 1] == second[j - 2] and first[i - 2] == second[j - 1]:
                current[j] = min(current[j], before[j - 2] + _SWAPPED)
        if min(current) > limit:
            return limit + 1
        before, previous = previous, current
    return round(previous[-1], 2)


@lru_cache(maxsize=100000)
def _word_edits(typed: str, catalog: str) -> float | None:
    """Edits between two sound keys, or None when over the typo budget for their length.

    Words under 4 letters must match; longer words allow one typo, 8+ letters two.
    Numbers must match exactly.
    """
    if typed == catalog:
        return 0
    if _numbers(typed) or _numbers(catalog):
        return None
    # The article is not part of the word's length: "الرضوانيه" is a 7-letter word with
    # a one-typo budget, not a 9-letter one that would reach "الرحمانية" with two.
    typed = typed[2:] if typed.startswith("ال") else typed
    catalog = catalog[2:] if catalog.startswith("ال") else catalog
    if typed == catalog:
        return 0
    longest = max(len(typed), len(catalog))
    budget = 0 if longest < 4 else 1 if longest < 8 else 2
    if budget == 0:
        return _short_slip(typed, catalog)
    edits = _edits(typed, catalog, budget)
    return edits if edits <= budget else None


def _short_slip(typed: str, catalog: str) -> float | None:
    """Edits of a 2-3 letter word typed with one dropped letter or two neighbours swapped.

    "الرب" for "العرب", "السحن" for "الحسن", "الصن" for "النص". A replaced or extra letter
    stays a different word at this length ("حسن"/"حسين", "حسن"/"حسب").
    """
    if len(typed) < 2:
        return None
    if len(catalog) == len(typed) + 1 and any(
            catalog[:i] + catalog[i + 1:] == typed for i in range(len(catalog))):
        return _MISSING_OR_EXTRA
    if len(catalog) == len(typed):
        changed = [i for i, (a, b) in enumerate(zip(typed, catalog)) if a != b]
        if (len(changed) == 2 and changed[1] == changed[0] + 1
                and typed[changed[0]] == catalog[changed[1]] and typed[changed[1]] == catalog[changed[0]]):
            return _SWAPPED
    return None


def _tokens(text: str) -> list[tuple[int, int, str]]:
    """Word spans of the original text with their spelling keys; separators are skipped."""
    return [(m.start(), m.end(), key) for m in _TOKEN.finditer(text or "") if (key := word_key(m.group()))]


def _strip_prefix(text: str, district: str) -> str | None:
    """Remove one leading district only, preserving all other address text."""
    target = phrase_key(district)
    count = len(target.split())
    tokens = _tokens(text)
    if not count or len(tokens) < count:
        return None
    if " ".join(key for _, _, key in tokens[:count]) != target:
        return None
    return text[tokens[count - 1][1]:].lstrip(_EDGES).rstrip()


def _loose_compact(value: str) -> str:
    return loose_key(phrase_key(value)).replace(" ", "")


def _group(names: list[str], key_function) -> dict[str, set]:
    groups: dict[str, set] = defaultdict(set)
    for name in names:
        key = key_function(name)
        if key:
            groups[key].add(name)
    return groups


def _place_key(name: str) -> str:
    """"حي ميثم تمار" and "ميثم / التمار" are one neighborhood; "مجمع X" may be a different place."""
    words = phrase_key(name).split()
    return "".join(words[1:] if len(words) > 1 and words[0] == "حي" else words)


def _same_place(names) -> bool:
    """The catalog lists one place several ways ("حي ميثم تمار" / "ميثم / التمار")."""
    return len({_place_key(name) for name in names}) == 1


@lru_cache(maxsize=10000)
def _center_parts(name: str) -> tuple[str, ...]:
    """The center/governorate phrases that make up `name`, or () when another word is in it."""
    words = phrase_key(name).split()
    parts, position = [], 0
    while position < len(words):
        size = next((size for size in (2, 1) if position + size <= len(words)
                     and " ".join(words[position:position + size]) in _CENTERS), 0)
        if not size:
            return ()
        parts.append(" ".join(words[position:position + size]))
        position += size
    return tuple(parts)


def _own_centers(state_names) -> frozenset:
    return frozenset().union(*(_STATE_CENTERS.get(phrase_key(name), ()) for name in state_names or () if name))


def is_center(name: str, state_names=()) -> bool:
    """This governorate's name or center, alone or combined ("الناصرية ذي قار", "الانبار رمادي").

    With the governorate known, another governorate's name is a neighborhood here: "تكريت قادسية"
    and "القادسية" in صلاح الدين, "صلاح الدين" in اربيل.
    """
    parts = _center_parts(name)
    own = _own_centers(state_names)
    if own:
        return bool(parts) and all(part in own for part in parts)
    return len(parts) == 1


def _choose(names, text: str) -> str | None:
    """The single name, or the spelling closest to the text among duplicate catalog entries."""
    if len(names) == 1:
        return next(iter(names))
    if not names or not _same_place(names):
        return None
    for key_function in (surface_key, normalize):
        target = key_function(text)
        scored = sorted(((_ratio(target, key_function(name)), name) for name in names), reverse=True)
        if scored[0][0] > scored[1][0]:
            return scored[0][1]
    return None


class _Index:
    """Lookup tables for one company/state district list."""

    def __init__(self, names: list[str]):
        self.size = len(names)
        self.by_normalized = _group(names, normalize)
        self.by_key = _group(names, matching_key)
        self.by_phrase = _group(names, phrase_key)
        self.by_compact = _group(names, compact_key)
        # Final alef read as taa marbuta ("عين كاوه" -> "عينكاوا"), tried after the stricter keys.
        self.by_loose = _group(names, _loose_compact)
        self.by_ordinal = _group(names, lambda name: ordinal_key(phrase_key(name).split()))
        # Names without their own leading label: "اسكان موانئ" -> {"جمعية اسكان الموانئ"}.
        self.by_label_core: dict[str, set] = defaultdict(set)
        # Two-part names in the other order: "حي الداخلية / اليرموك" <- "اليرموك الداخلية".
        self.by_reversed: dict[str, set] = defaultdict(set)
        # Longer names by their leading words: "جامعه" -> [(1, "جامعه بصره", "جامعة البصرة"), ...].
        self.extensions: dict[str, list] = defaultdict(list)
        fuzzy: dict[int, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        for name in names:
            words = phrase_key(name).split()
            core = without_label(words)
            if core:
                self.by_label_core[" ".join(core)].add(name)
            parts = [part.split() for part in map(phrase_key, name.split("/")) if part]
            if len(parts) == 2:
                first, second = (without_label(part) or part for part in parts)
                self.by_reversed[" ".join(second + first)].add(name)
            for key_words in filter(None, (words, core)):
                fuzzy[len(key_words)][" ".join(key_words)].add(name)
                for size in range(1, len(key_words)):
                    self.extensions[" ".join(key_words[:size])].append(
                        (len(key_words) - size, " ".join(key_words), name))
        self.max_words = max(fuzzy, default=0)
        self.fuzzy_by_words = {count: list(items.items()) for count, items in fuzzy.items()}
        self.candidate_keys = [(count, key, key.replace(" ", ""), names)
                               for count, items in fuzzy.items() for key, names in items.items()]
        self.whole = [(matching_key(name), name) for name in names]
        self.phrases = [(phrase_key(name), name) for name in names]
        # Sound keys per word, with and without the name's own label, for the typo tier.
        typo: dict[int, list] = defaultdict(list)
        self.vocabulary: set[str] = set()
        for name in names:
            parts = [match.group() for match in _TOKEN.finditer(name)]
            self.vocabulary.update(filter(None, map(word_key, parts)))
            sounds = [sound_key(part) for part in parts if word_key(part)]
            forms = [sounds]
            if len(sounds) > 1 and word_key(parts[0]) in GENERIC_LABELS:
                forms.append(sounds[1:])
            for form in forms:
                typo[len(form)].append((form, name))
        self.typo_by_words = dict(typo)


_INDEXES: dict[int, tuple[list, _Index]] = {}


def _index(allowed: list[dict]) -> _Index:
    # Keyed by list identity; the stored reference keeps the id from being reused.
    cached = _INDEXES.get(id(allowed))
    if cached is not None and cached[0] is allowed and cached[1].size == len(allowed):
        return cached[1]
    if len(_INDEXES) > 512:
        _INDEXES.clear()
    index = _Index([entry["name"] for entry in allowed])
    _INDEXES[id(allowed)] = (allowed, index)
    return index


def _state_phrases(state_names) -> list[list[str]]:
    phrases = {tuple(phrase_key(name).split()) for name in state_names or () if name}
    return [list(phrase) for phrase in sorted(phrases, key=len, reverse=True) if phrase]


def _governorate_length(keys: list[str], start: int, phrases: list[list[str]]) -> int:
    """Length of a governorate reference ("واسط", "محافظة واسط") starting at `start`."""
    position = start + (start < len(keys) and keys[start] == GOVERNORATE_LABEL)
    for phrase in phrases:
        if keys[position:position + len(phrase)] == phrase:
            return position + len(phrase) - start
    return 0


def _clean_remainder(text: str, phrases: list[list[str]], trailing_phrases: list[list[str]] | None = None) -> str:
    """Trim separators and governorate references from the edges of moved text.

    `trailing_phrases` (default: `phrases`) limits what is dropped from the end.
    """
    trailing_phrases = phrases if trailing_phrases is None else trailing_phrases
    text = text.strip(_EDGES)
    while text:
        tokens = _tokens(text)
        if not tokens:
            return ""
        keys = [key for _, _, key in tokens]
        leading = _governorate_length(keys, 0, phrases)
        if leading:
            text = text[tokens[leading - 1][1]:].strip(_EDGES)
            continue
        trailing = next((size for size in range(1, len(tokens) + 1) if
                         _governorate_length(keys, len(tokens) - size, trailing_phrases) == size), 0)
        # "فندق بابل" / "مستشفى بغداد" name a landmark, not the governorate.
        if not trailing or (len(tokens) > trailing and keys[len(tokens) - trailing - 1] in ADDRESS_WORDS):
            return text
        text = text[:tokens[len(tokens) - trailing][0]].strip(_EDGES)
    return text


def _response(case: CaseRequest, district: str, details: str, confidence: float,
              status: str, reason: str) -> CaseResponse:
    return CaseResponse(excelSequence=case.excelSequence, originalDistrict=case.district,
                        correctDistrict=district, addressDetails=details,
                        confidence=confidence, status=status, reason=reason,
                        stateCode=case.stateCode.strip().upper())


def unresolved(case: CaseRequest, reason: str = "No reliable catalog match; original district kept.",
               error: str | None = None) -> CaseResponse:
    result = _response(case, case.district, case.address, 0.2, "UNRESOLVED", reason)
    result.errorCode = error
    return result


def _details(case: CaseRequest, name: str, remainder: str = "") -> str:
    """Moved district text first, then the address without a repeated district prefix."""
    tail = _strip_prefix(case.address, name)
    address = (case.address if tail is None else tail).strip()
    if remainder and address:
        moved, kept = " " + normalize(remainder) + " ", " " + normalize(address) + " "
        if kept in moved:
            address = ""
        elif moved in kept:
            remainder = ""
    return " ".join(part for part in (remainder, address) if part)


def _full_name(index: _Index, words: list[str], phrases: list[list[str]]) -> set | None:
    """A catalog name spelled like these words, or a labeled name without its label."""
    phrase = " ".join(words)
    found = (index.by_phrase.get(phrase) or index.by_compact.get(phrase.replace(" ", ""))
             or index.by_loose.get(_loose_compact(phrase)) or index.by_ordinal.get(ordinal_key(words)))
    if found or words in phrases:  # a bare governorate name is not "حي <governorate>"
        return found
    found = index.by_label_core.get(phrase) or index.by_reversed.get(phrase)
    # "الدورة حي الوادي" is "الدورة - الوادي": a label inside the text, not in the name.
    inner = [word for position, word in enumerate(words)
             if position in (0, len(words) - 1) or word not in GENERIC_LABELS]
    # Not after a governorate or center: "بابل حي الحسين" is "حي الحسين", not "بابل حسين".
    if not found and len(inner) < len(words) and [words[0]] not in phrases and words[0] not in _CENTERS:
        found = index.by_phrase.get(" ".join(inner)) or index.by_compact.get("".join(inner))
    return found


def _longest(index: _Index, keys: list[str], start: int, phrases: list[list[str]], label_dropped: bool):
    """(end, names, matched words) of the longest name at `start`."""
    for length in range(min(len(keys) - start, index.max_words + 1), 0, -1):
        words = keys[start:start + length]
        if label_dropped:
            words = without_label(words)
            if not words or words in phrases:
                continue
        names = _full_name(index, words, phrases)
        if names:
            return start + length, names, words
    return None


def _window_match(index: _Index, keys: list[str], starts: list[int], phrases: list[list[str]]):
    """Longest name at each start; the match reaching furthest into the field wins.

    Returns (end, start, names, matched words). The text's own label ("حي", "مجمع")
    is only dropped when that explains more of the field; if both readings fit
    ("حي بابل" + "حسين" or "بابل حسين"), the names are returned together as ambiguous.
    """
    best = None
    for start in starts:
        with_label = _longest(index, keys, start, phrases, label_dropped=False)
        without = _longest(index, keys, start, phrases, label_dropped=True)
        if with_label and without and without[0] > with_label[0] and without[1] != with_label[1]:
            found = (without[0], with_label[1] | without[1], without[2])
        else:
            found = with_label or without
        if found and (best is None or (found[0], -start) > (best[0], -best[1])):
            best = (found[0], start, found[1], found[2])
    return best


def _fuzzy_best(index: _Index, key: str, words: int):
    """(score, runner-up score, names, catalog key) of the closest names with the same word count."""
    scores = [(_similarity(key, candidate), candidate, names)
              for candidate, names in index.fuzzy_by_words.get(words, ()) if _may_be_similar(key, candidate)]
    if not scores:
        return None
    scores.sort(key=lambda item: item[0], reverse=True)
    best_score, best_key, best_names = scores[0]
    runner = next((score for score, _, names in scores[1:] if not _same_place(names | best_names)), 0.0)
    return best_score, runner, best_names, best_key


def _window_fuzzy(index: _Index, keys: list[str], starts: list[int], phrases: list[list[str]]):
    """Misspelled district at the start of the field ("عنكاوا - خلف فندق هيكسوس").

    Returns (end, start, score, names, catalog key words), AMBIGUOUS, or None. A longer
    window that fits several names blocks shorter windows at the same start, so
    "مجمع الامرات الثاني" never falls back to the shorter "مجمع الاميرات".
    """
    best = None
    for start in starts:
        found_here = None
        for length in range(min(len(keys) - start, index.max_words), 0, -1):
            if start == 0 and length == len(keys):
                continue  # the whole field was already judged by the whole-string fuzzy stage
            words = keys[start:start + length]
            if words in phrases:
                continue
            for option in filter(None, (words, without_label(words))):
                if len(" ".join(option).replace(" ", "")) < _MIN_FUZZY_KEY:
                    continue
                found = _fuzzy_best(index, " ".join(option), len(option))
                if found is None or found[0] < _FUZZY_ACCEPT:
                    continue
                if found[0] - found[1] < _FUZZY_MARGIN or not _same_place(found[2]):
                    found_here = AMBIGUOUS
                elif found_here is not AMBIGUOUS and (found_here is None or found[0] > found_here[2]):
                    found_here = (start + length, start, found[0], found[2], found[3].split())
            if found_here is not None:
                break
        if found_here is AMBIGUOUS:
            return AMBIGUOUS
        if found_here and (best is None or (found_here[0], -start) > (best[0], -best[1])):
            best = found_here
    return best


# Standard Arabic keyboard rows; a slip hits a neighbouring key ("زاتكو"/"زانكو", "زيزنه"/"زيونة").
_KEY_ROWS = ("ضصثقفغعهخحجد", "شسيبلاتنمكط", "ئءؤرىةوزظ")


def _keyboard_pairs() -> frozenset:
    pairs = set()
    for row in _KEY_ROWS:
        pairs.update(zip(row, row[1:]))
    for upper, lower in zip(_KEY_ROWS, _KEY_ROWS[1:]):
        pairs.update(zip(upper, lower))
    # Compared words are sound keys, so pair the sound-alike forms too.
    pairs.update((sound_key(a) or a, sound_key(b) or b) for a, b in list(pairs))
    return frozenset(pairs | {(b, a) for a, b in pairs})


_NEIGHBOUR_KEYS = _keyboard_pairs()


def _short(typed: str, catalog: str) -> bool:
    """A short word (under 6 letters, the article aside) with a replaced letter that is no slip.

    A replaced letter in a short word is another real word as often as a typo ("شركة"/"شرطة",
    "حطين"/"حسين"). Kept: the last letter ("الاوله"/"الاولى") or a neighbouring key.
    """
    typed, catalog = (word[2:] if word.startswith("ال") else word for word in (typed, catalog))
    if max(len(typed), len(catalog)) >= 6:
        return False
    if len(typed) != len(catalog):
        return True
    changed = [(a, b) for a, b in zip(typed, catalog) if a != b]
    return not (len(changed) == 1 and (typed[:-1] == catalog[:-1] or changed[0] in _NEIGHBOUR_KEYS))


def _window_typo(index: _Index, sounds: list[str], keys: list[str], starts: list[int],
                 phrases: list[list[str]], strict: frozenset = frozenset()):
    """Catalog name typed with small mistakes in its words ("ساةح سعد", "دور الظباط", "مسفى بيجي").

    Each word may differ by its typo budget (swapped, missing, extra or sound-alike letters).
    The name with the fewest edits wins; different places tied at the fewest edits are
    AMBIGUOUS. Starts in `strict` accept no replaced letter ("حي الشركة" is not "الشرطة").
    Returns (end, start, names), AMBIGUOUS, or None.
    """
    best = None
    for start in starts:
        found_here = None
        for length in range(min(len(sounds) - start, index.max_words), 0, -1):
            if keys[start:start + length] in phrases or _SIDES.intersection(keys[start:start + length]):
                continue
            window = sounds[start:start + length]
            if sum(map(len, window)) < 4:
                continue
            edits: dict[str, float] = {}
            for form, name in index.typo_by_words.get(length, ()):
                total = 0
                for typed, typed_key, catalog in zip(window, keys[start:start + length], form):
                    word = _word_edits(typed, catalog)
                    # A replaced letter in a short word is another real word as often as a typo
                    # ("شركة"/"شرطة", "الرصافة"/"الرسالة"); only longer words allow it.
                    if word is None or (word and typed_key in _FACILITIES) or (
                            word >= _REPLACED and _short(typed, catalog)):
                        break
                    total += word
                else:
                    if start not in strict or total < _REPLACED:
                        edits[name] = min(total, edits.get(name, total))
            if edits:
                fewest = min(edits.values())
                names = {name for name, total in edits.items() if total == fewest}
                found_here = (start + length, start, names) if _same_place(names) else AMBIGUOUS
                break
        if found_here is AMBIGUOUS:
            return AMBIGUOUS
        if found_here and (best is None or (found_here[0], -start) > (best[0], -best[1])):
            best = found_here
    return best


def _resegment(raw: str, index: _Index, phrases: list[list[str]]) -> str:
    """Insert the missing space in glued words ("رواندزقرب" -> "رواندز قرب").

    A word is split only when it is unknown and both parts are known words: catalog
    words, governorate words, labels ("قضاءالدور") or common address words.
    """
    known = (index.vocabulary | ADDRESS_WORDS | GENERIC_LABELS | {GOVERNORATE_LABEL}
             | {word for phrase in phrases for word in phrase})
    pieces, last = [], 0
    for match in _TOKEN.finditer(raw):
        token = match.group()
        if word_key(token) in known or len(token) < 5:
            continue
        for cut in range(len(token) - 2, 2, -1):
            left, right = word_key(token[:cut]), word_key(token[cut:])
            if left in known and right in known and len(left) >= 3 and len(right) >= 2:
                pieces += [raw[last:match.start() + cut], " "]
                last = match.start() + cut
                break
    return "".join(pieces) + raw[last:]


def _whole_fuzzy(index: _Index, raw: str):
    target = matching_key(raw)
    scores = sorted(((_similarity(target, key), name) for key, name in index.whole
                     if _may_be_similar(target, key)), reverse=True)
    if scores:
        best_score, best_name = scores[0]
        runner = next((score for score, name in scores[1:] if not _same_place({name, best_name})), 0.0)
        if best_score >= _FUZZY_ACCEPT and best_score - runner >= _FUZZY_MARGIN:
            return best_score, {name for score, name in scores
                                if score == best_score and _same_place({name, best_name})}
    return None


def _extension(index: _Index, matched: list[str], keys: list[str], end: int):
    """A longer catalog name whose extra words match the following text despite a typo.

    "جامعة البرة" first matches "حي الجامعة" by "جامعة"; "جامعة البصرة" explains
    the next word too. Returns (score, extra words, names), None when no longer name
    fits, or AMBIGUOUS when several longer names fit about equally.
    """
    scores = []
    for extra, key, candidate in index.extensions.get(" ".join(matched), ()):
        if end + extra > len(keys):
            continue
        following = keys[end:end + extra]
        extra_key = " ".join(key.split()[len(matched):])
        if _similarity(" ".join(following), extra_key) >= _EXTRA_WORDS_ACCEPT:
            scores.append((_similarity(" ".join(matched + following), key), extra, candidate))
    if not scores:
        return None
    scores.sort(reverse=True)
    best_score, extra, best_name = scores[0]
    runner = next((score for score, _, name in scores[1:] if not _same_place({name, best_name})), 0.0)
    if best_score < _FUZZY_ACCEPT:
        return None
    if best_score - runner < _FUZZY_MARGIN:
        return AMBIGUOUS
    return best_score, extra, {name for score, count, name in scores
                               if score == best_score and count == extra and _same_place({name, best_name})}


def _split(case: CaseRequest, raw: str, tokens, end: int, name: str, phrases, confidence: float,
           reason: str, fuzzy: bool = False) -> CaseResponse:
    """SPLIT_ADDRESS when text remains after the district, otherwise a plain match."""
    remainder = _clean_remainder(raw[tokens[end - 1][1]:], phrases)
    if remainder:
        return _response(case, name, _details(case, name, remainder), confidence, "SPLIT_ADDRESS", reason)
    if fuzzy:
        return _response(case, name, _details(case, name), confidence, "FUZZY_MATCH", reason)
    return _response(case, name, _details(case, name), 0.93, "NORMALIZED_MATCH", _WORDS_IGNORED_REASON)


def _inside_center(case: CaseRequest, index: _Index, state_names, raw: str, tokens, end: int,
                   name: str, phrases):
    """The district written after its governorate center ("الناصرية الشطرة" -> "الشطرة").

    The center is context, like the governorate. When the rest of the text names a
    catalog district exactly or by spelling only, that district is the answer. When the
    rest names a district through the typo tier (one budgeted typo, never a tie) or as the
    only name starting with those words, that district is also the answer. When the rest
    only resembles a district (fuzzy or ambiguous), returns _REVIEW so the center is kept
    at review confidence. Returns None when the rest names no district.
    """
    if not is_center(name, state_names):
        return None
    rest = raw[tokens[end - 1][1]:]
    while (rest_tokens := _tokens(rest)) and rest_tokens[0][2] in _HONORIFICS:
        rest = rest[rest_tokens[0][1]:]
    rest = _clean_remainder(rest, phrases)
    if not rest:
        return None
    inner = _match_text(case.model_copy(update={"district": rest}), index, state_names)
    if inner.status == "UNRESOLVED":
        # "بغداد الرضوانيه": the rest begins several names ("الرضوانية الشرقية / ...", "الرضوانية الغربية / ...").
        several = inner.reason == _AMBIGUOUS_REASON or index.extensions.get(phrase_key(rest))
        # "بغداد - رصافة معلمين": the words appear in several names ("حي المعلمين / الدورة",
        # "حي المعلمين / المشتل", ...); the center is kept only for review.
        words = {key for _, _, key in _tokens(rest) if key not in _SIDES and key not in GENERIC_LABELS}
        if not several and words:
            several = sum(words <= set(key.split()) for key, _ in index.phrases) > 1
        return _REVIEW if several else None
    if inner.correctDistrict == name:
        return None
    if ((inner.status in ("NORMALIZED_MATCH", "SPLIT_ADDRESS") and inner.confidence >= 0.93)
            or inner.reason in (_TYPO_REASON, _PREFIX_REASON)):
        reason = _INSIDE_CENTER_REASON if inner.confidence >= 0.93 else f"{_INSIDE_CENTER_REASON} {inner.reason}"
        return inner.model_copy(update={"originalDistrict": case.district, "reason": reason})
    return _REVIEW


def _unique_prefix(index: _Index, keys: list[str], start: int, phrases: list[list[str]]) -> set | None:
    """The one place whose name begins with all the written words ("حي الامن" -> "حي الامن الداخلي").

    The words must cover at least half of the name, and a governorate or its center
    never counts ("البصره" is not "البصرة القديمة"). Several places -> None.
    """
    words = keys[start:]
    phrase = " ".join(words)
    if not words or words in phrases or phrase in _CENTERS:
        return None
    names = {name for extra, _, name in index.extensions.get(phrase, ()) if extra <= len(words)}
    return names if names and _same_place(names) else None


def clean_details(text: str, state_names=()) -> str:
    """AI address details without edge governorate or center names, or a bare label.

    "ميسان" / "البصره قضاء" / "عمارة" carry no location the district does not already give.
    """
    phrases = sorted(_state_phrases(state_names) + [key.split() for key in _CENTERS], key=len, reverse=True)
    # At the end only this governorate's name: "خلف مركز شرطة القادسية" keeps القادسية in صلاح الدين.
    text = _clean_remainder(text or "", phrases, _state_phrases(state_names))
    # "النجف الاشرف حي الجامعة" leaves "الاشرف" behind once the center is gone.
    while (tokens := _tokens(text)) and tokens[0][2] in _HONORIFICS:
        text = text[tokens[0][1]:].strip(_EDGES)
    if all(key in GENERIC_LABELS or key == GOVERNORATE_LABEL for _, _, key in _tokens(text)):
        return ""
    return text


def _fuzzy_confidence(score: float) -> float:
    return round(min(0.9, 0.84 + (score - _FUZZY_ACCEPT) * 0.5), 2)


# District values that name no place; the address then carries it.
_PLACEHOLDERS = frozenset(phrase_key(word) for word in ("اخرى", "أخرى", "غير ذلك", "غير محدد", "other", "others"))
# Shop-export noise around addresses: "IQ-KI-AR", "Iraq", "Suite".
_ADDRESS_NOISE = re.compile(r"IQ-[A-Z]{2}-[A-Z]{2}|Iraq|Suite", re.I)


def _address_text(address: str) -> str:
    text = _ADDRESS_NOISE.sub(" ", html.unescape(address or ""))
    return " ".join(text.split()).strip(_EDGES)


def match_case(case: CaseRequest, allowed: list[dict], state_names=()) -> CaseResponse:
    """Never return a name outside the supplied company and state catalog."""
    index = _index(allowed)
    raw = case.district.strip()
    if not raw:
        return unresolved(case, "District is empty.")
    if phrase_key(raw) in _PLACEHOLDERS:
        text = _address_text(case.address)
        if not text:
            return unresolved(case, "District is a placeholder and the address names no place.")
        # "اخرى" with "ناصرية // ناحية الفضلية / قرب المستوصف" in the address.
        result = match_case(case.model_copy(update={"district": text, "address": ""}), allowed, state_names)
        if result.status == "UNRESOLVED":
            return unresolved(case, result.reason)
        return result.model_copy(update={"originalDistrict": case.district})
    plain = case.model_copy(update={"district": html.unescape(case.district), "address": html.unescape(case.address)})
    if plain != case:
        # Exported JSON may keep HTML entities ("نينوى &apos;سنجار").
        result = match_case(plain, allowed, state_names)
        if result.status == "UNRESOLVED":
            return unresolved(case, result.reason)
        return result.model_copy(update={"originalDistrict": case.district})

    if raw in index.by_normalized.get(normalize(raw), ()):
        details = _strip_prefix(case.address, raw)
        if details is not None and details != case.address:
            return _response(case, raw, details, 1.0, "SPLIT_ADDRESS", "Duplicate district prefix removed from address.")
        return _response(case, raw, case.address, 1.0, "EXACT_MATCH", "Exact company and state catalog match.")

    spaced = _resegment(raw, index, _state_phrases(state_names))
    if spaced != raw:
        result = _match_text(case.model_copy(update={"district": spaced}), index, state_names)
        if result.status == "UNRESOLVED":
            return unresolved(case, result.reason)
        return result.model_copy(update={"originalDistrict": case.district})
    return _match_text(case, index, state_names)


def _match_text(case: CaseRequest, index: _Index, state_names) -> CaseResponse:
    raw = case.district.strip()

    ambiguous = False
    for groups, key_function, confidence in ((index.by_normalized, normalize, 0.97),
                                             (index.by_key, matching_key, 0.95),
                                             (index.by_phrase, phrase_key, 0.95),
                                             (index.by_compact, compact_key, 0.95)):
        found = groups.get(key_function(raw), ())
        name = _choose(found, raw)
        if name:
            return _response(case, name, _details(case, name), confidence, "NORMALIZED_MATCH",
                             _SPELLING_REASON if len(found) == 1 else _DUPLICATE_REASON)
        ambiguous = ambiguous or len(found) > 1

    phrases = _state_phrases(state_names)
    tokens = _tokens(raw)
    keys = [key for _, _, key in tokens]
    # Context words before the district: the governorate, its center ("الناصريه / الشطره"),
    # an honorific ("النجف الاشرف") or a Baghdad bank ("الرصافة"), in any combination.
    centers = _own_centers(state_names)
    starts, skipped = [0], 0
    while skipped < len(keys):
        step = (_governorate_length(keys, skipped, phrases)
                or next((size for size in (2, 1) if " ".join(keys[skipped:skipped + size]) in centers
                         and skipped + size <= len(keys)), 0)
                or int(keys[skipped] in _SIDES or keys[skipped] in _HONORIFICS))
        if not step or skipped + step >= len(keys):
            break
        skipped += step
        starts.append(skipped)

    def text(start: int, end: int) -> str:
        return raw[tokens[start][0]:tokens[end - 1][1]]

    def split(end: int, name: str, confidence: float, reason: str, fuzzy: bool = False) -> CaseResponse:
        inner = _inside_center(case, index, state_names, raw, tokens, end, name, phrases)
        if isinstance(inner, CaseResponse):
            return inner
        result = _split(case, raw, tokens, end, name, phrases, confidence, reason, fuzzy)
        if inner is _REVIEW:
            result = result.model_copy(update={
                "confidence": min(result.confidence, CENTER_REVIEW),
                "reason": result.reason + " The text after the center may name a more specific district."})
        return result

    window = _window_match(index, keys, starts, phrases)
    if window is not None:
        end, start, names, matched = window
        longer = _extension(index, matched, keys, end)
        name = _choose(names, text(start, end))
        if name is None or longer is AMBIGUOUS:
            # Several different districts fit equally well; an arbitrary pick could be the wrong ID.
            return unresolved(case, _AMBIGUOUS_REASON)
        if longer is not None:
            score, extra, names = longer
            name = _choose(names, text(start, end + extra))
            if name is None:
                return unresolved(case, _AMBIGUOUS_REASON)
            return split(end + extra, name, _fuzzy_confidence(score),
                         "Longer catalog district matched despite a spelling difference.", fuzzy=True)
        return split(end, name, 0.94, "Catalog district extracted from start of district field.")
    if ambiguous:
        return unresolved(case, _AMBIGUOUS_REASON)

    for start in starts:
        names = _unique_prefix(index, keys, start, phrases)
        if names and (name := _choose(names, text(start, len(keys)))):
            return _response(case, name, _details(case, name), 0.9, "FUZZY_MATCH", _PREFIX_REASON)

    sounds = [sound_key(raw[start:end]) for start, end, _ in tokens]
    # The typed label is not part of the misspelled name ("منطقه الداوددي" -> "الداودي").
    after_label = frozenset(start + 1 for start in starts
                            if start + 1 < len(keys) and keys[start] in GENERIC_LABELS) - set(starts)
    typo = _window_typo(index, sounds, keys, starts + sorted(after_label), phrases, after_label)
    if typo is AMBIGUOUS:
        return unresolved(case, _AMBIGUOUS_REASON)
    if typo is not None:
        end, start, names = typo
        name = _choose(names, text(start, end))
        if name is not None:
            return split(end, name, 0.9, _TYPO_REASON, fuzzy=True)

    whole = _whole_fuzzy(index, raw)
    if whole is not None and (name := _choose(whole[1], raw)):
        score = whole[0]
        return _response(case, name, _details(case, name),
                         round(min(0.93, 0.85 + (score - 0.89) * 0.5), 2),
                         "FUZZY_MATCH", "Unique high-similarity catalog match.")

    fuzzy = _window_fuzzy(index, keys, starts, phrases)
    if fuzzy is AMBIGUOUS:
        return unresolved(case, _AMBIGUOUS_REASON)
    if fuzzy is not None:
        end, start, score, names, matched = fuzzy
        longer = _extension(index, matched, keys, end)
        if longer is AMBIGUOUS:
            return unresolved(case, _AMBIGUOUS_REASON)
        if longer is not None:
            score, extra, names = longer
            end += extra
        name = _choose(names, text(start, end))
        if name is None:
            return unresolved(case, _AMBIGUOUS_REASON)
        return split(end, name, _fuzzy_confidence(score),
                     "Misspelled catalog district matched at start of district field.", fuzzy=True)
    return unresolved(case)


def text_support(text: str, name: str) -> float:
    """How closely the text spells `name` (0..1): best window similarity, by letters and by sound.

    An AI pick is only trusted when the text actually writes it, even with typos or glued
    words. A plausible-sounding district that the text never mentions ("البصره" -> "الزبير")
    scores low. Real typos in the hard-case set score 0.8 or more; such picks 0.71 or less.
    """
    keys = [key for _, _, key in _tokens(text)]
    glued = "".join(keys)
    words = phrase_key(name).split()
    best = 0.0
    for form in filter(None, (words, without_label(words))):
        target = " ".join(form)
        compact = target.replace(" ", "")
        if len(compact) >= _MIN_FUZZY_KEY and (compact in glued or sound_key(compact) in sound_key(glued)):
            return 1.0
        for length in range(1, len(keys) + 1):
            for start in range(len(keys) - length + 1):
                window = " ".join(keys[start:start + length])
                joined = window.replace(" ", "")
                best = max(best, _similarity(target, window), _similarity(compact, joined),
                           _similarity(sound_key(compact), sound_key(joined)))
    return best


def without_governorate(text: str, state_names=()) -> str:
    """The text without the governorate or its name at its edges ("بغداد - اليرموك" -> "اليرموك")."""
    return _clean_remainder(html.unescape(text or ""), _state_phrases(state_names))


def memory_key(text: str, state_names=()) -> str:
    """Key of a confirmed correction: spelling-normalized words, governorate dropped.

    "بغداد اليرموك مقابل جامع الشواف" and "اليرموك مقابيل جامع الشواف" differ, but
    "بغداد / اليرموك مقابل جامع الشواف" and "اليرموك مقابل جامع الشوّاف" share one key.
    """
    return phrase_key(without_governorate(text, state_names))


# Places whose names reuse a district's name: "مركز الشرطه" is not "حي الشرطة".
_FACILITIES = frozenset(word_key(word) for word in (
    "مركز", "مستشفى", "جامع", "مسجد", "مدرسة", "مول", "فندق", "مطعم", "دائرة", "سوق", "كلية", "معهد",
    "مصرف", "شركة", "صيدلية"))


def _cut_district(raw: str, name: str, phrases) -> str | None:
    """`raw` without the first place it writes `name` (with its label), or None when it does not.

    An occurrence after a street or facility word names that street or facility
    ("شارع الجزائر") and stays.
    """
    tokens = _tokens(raw)
    keys = [key for _, _, key in tokens]
    target = phrase_key(name).split()
    for form in filter(None, (target, without_label(target))):
        for start in range(len(keys) - len(form) + 1):
            if keys[start:start + len(form)] != form:
                continue
            before = keys[start - 1] if start else None
            if before in GENERIC_LABELS:
                begin = start - 1
            elif before in ADDRESS_WORDS or before in _FACILITIES:
                continue
            else:
                begin = start
            parts = (_clean_remainder(raw[:tokens[begin][0]], phrases),
                     _clean_remainder(raw[tokens[start + len(form) - 1][1]:], phrases))
            return " ".join(part for part in parts if part)
    return None


def without_district(details: str, name: str, state_names=()) -> str:
    """AI address details without the chosen district written again ("حي الإعلام الشباب" -> "الشباب")."""
    remainder = _cut_district(details, name, _state_phrases(state_names))
    return details if remainder is None else remainder


def remembered_details(case: CaseRequest, name: str, state_names=()) -> tuple[str, bool]:
    """(address details, moved) for a confirmed district: the text without the district.

    When the text does not write the district (a learned spelling such as "حاكماوه"), the
    text itself is kept as details so no location is lost.
    """
    phrases = _state_phrases(state_names)
    raw = html.unescape(case.district).strip()
    plain = case.model_copy(update={"district": raw, "address": html.unescape(case.address)})
    remainder = _cut_district(raw, name, phrases)
    if remainder is None:
        remainder = _clean_remainder(raw, phrases)
    return _details(plain, name, remainder), bool(remainder)


def _written(typed: list[str], word: str) -> bool:
    return any(_word_edits(sound, word) is not None for sound in typed)


def _sound_words(name: str) -> list[str]:
    parts = [part for part in _TOKEN.findall(name) if word_key(part)]
    if len(parts) > 1 and word_key(parts[0]) in GENERIC_LABELS:
        parts = parts[1:]
    return [sound_key(word_key(part)) for part in parts]


def adds_unwritten(text: str, name: str, base: str) -> bool:
    """`name` is `base` plus words the text never writes ("شارع فلسطين" -> "الادريسي / شارع فلسطين").

    A longer name is kept when the text writes at least one of its extra words
    ("الدورة الميكانيك" -> "دورة ميكانيك واسيا").
    """
    base_words, name_words = _sound_words(base), _sound_words(name)
    extra = [word for word in name_words if word not in base_words]
    if not base_words or not extra or not set(base_words) <= set(name_words):
        return False
    typed = [sound_key(key) for _, _, key in _tokens(text)]
    return not any(_written(typed, word) for word in extra)


def named_as_facility(text: str, name: str) -> bool:
    """The pick is a labeled place ("حي الشرطة") the text writes only inside a facility ("مركز الشرطه")."""
    core = without_label(phrase_key(name).split())
    if not core:
        return False
    keys = [key for _, _, key in _tokens(text)]
    found = [start for start in range(len(keys) - len(core) + 1) if keys[start:start + len(core)] == core]
    return bool(found) and all(start and keys[start - 1] in _FACILITIES for start in found)


def written_edits(text: str, name: str) -> float | None:
    """Fewest typo edits with which the text writes `name` word by word, or None.

    Its label is optional ("حي الجهاد" is written by "الجهاد"). Lets a closer spelling
    found by the rules ("الحيرية" -> "الحيدرية", one dropped letter) win over an AI pick
    the text writes only with a costlier typo ("الحيرة", one stray extra letter).
    """
    typed = [sound_key(key) for _, _, key in _tokens(text)]
    full = [sound_key(word_key(part)) for part in _TOKEN.findall(name) if word_key(part)]
    best = None
    for form in filter(None, (full, _sound_words(name))):
        for start in range(len(typed) - len(form) + 1):
            total = 0.0
            for sound, word in zip(typed[start:], form):
                edits = _word_edits(sound, word)
                if edits is None:
                    break
                total += edits
            else:
                best = total if best is None else min(best, total)
    return best


def contradicts(text: str, name: str) -> bool:
    """The text writes the start of `name`, then a different word where the rest should be.

    "الدورة ابو دشير" is not "دورة أبو طيارة" and "بعقوبه التحرير" is not "بعقوبه التربيه",
    although text_support scores both high. A name whose remaining words the text simply
    leaves out ("الدورة الميكانيك" -> "دورة ميكانيك واسيا") or follows with an address word
    ("قرب", "شارع") is not contradicted.
    """
    typed = [(key, sound_key(text[start:end])) for start, end, key in _tokens(text)]
    parts = [part for part in _TOKEN.findall(name) if word_key(part)]
    if len(parts) > 1 and word_key(parts[0]) in GENERIC_LABELS:
        parts = parts[1:]
    previous = None
    for word in map(sound_key, parts):
        found = [position for position, (_, sound) in enumerate(typed) if _word_edits(sound, word) is not None]
        if not found:
            following = None if previous is None or previous + 1 >= len(typed) else typed[previous + 1][0]
            return following is not None and following not in ADDRESS_WORDS and following not in GENERIC_LABELS
        previous = next((position for position in found if previous is None or position > previous), found[0])
    return False


def candidate_names(text: str, allowed: list[dict], limit: int = 20) -> list[str]:
    """Shortlist for the LLM: catalog names closest to any run of words in the text.

    Names are compared with and without their own label ("جمعية اسكان الموانئ" also as
    "اسكان الموانئ"), so a district typed without its label still reaches the shortlist.
    """
    index = _index(allowed)
    keys = [key for _, _, key in _tokens(_resegment(text, index, []))]
    if not keys or limit <= 0:
        return []
    windows: dict[int, set] = defaultdict(set)
    for length in range(1, min(len(keys), index.max_words) + 1):
        for start in range(len(keys) - length + 1):
            windows[length].add(" ".join(keys[start:start + length]))
    whole = {" ".join(keys)}
    compact_windows = {count: {window.replace(" ", "") for window in values}
                       for count, values in windows.items()}
    scores = dict.fromkeys((entry["name"] for entry in allowed), 0.0)
    # Score each shared spelling key once, rather than once per catalog variant.
    for count, key, compact, names in index.candidate_keys:
        best = 0.0
        for window in windows.get(count) or whole:
            if _may_be_similar(key, window) or count == 1:
                best = max(best, _similarity(key, window))
        # Missing spaces can collapse several district words into a single token.
        # Compare those windows without spaces while keeping the numeric constraint.
        for shorter in range(1, count):
            for window in compact_windows.get(shorter, ()):
                if _may_be_similar(compact, window):
                    best = max(best, _similarity(compact, window),
                               _similarity(sound_key(compact), sound_key(window)))
        for name in names:
            scores[name] = max(scores[name], best)
    return sorted(scores, key=scores.get, reverse=True)[:limit]
