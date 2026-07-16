# -*- coding: utf-8 -*-
"""مسترجع مواقع العراق (محافظات + مناطق) فوق locations.json.

المصدر ملفا states.xlsx وdistricts.xlsx من نظام شركة التوصيل (18 محافظة،
~4900 منطقة). بعد أي تحديث للملفين أعد التوليد بـ:

    python -m app.rag.prepare_locations

الاستخدامان:
- search_locations(text): مطابقة نص الزبون الخام مع أسماء المناطق/المحافظات
  لحقنها كسياق RAG في برومت استخراج الطلب (منطقة → محافظتها الصحيحة).
- state_for_district / canonical_state: تصحيح حتمي بعد الاستخراج — إذا
  المنطقة المستخرجة معروفة وتتبع محافظة واحدة، نعتمد محافظتها بدل تخمين
  الموديل، ونوحّد إملاء المحافظة على اسمها الرسمي بقاعدة البيانات.
"""

import difflib
import json
import os
from collections import defaultdict
from typing import List, Optional

from .retriever import tokenize

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOCATIONS_PATH = os.path.join(BASE_DIR, "locations.json")

# أطول اسم منطقة بعد التطبيع ~4 كلمات — لا داعي لفحص n-grams أطول
_MAX_NGRAM = 4
# عتبة التشابه للمطابقة الغيمية (difflib ratio) — أقل منها = تجاهل
_FUZZY_CUTOFF = 0.86
# للمحافظات عتبة أوطأ: 18 اسماً فقط والأخطاء الإملائية الشائعة أبعد من 0.86
# ("دواينه" ↔ "ديوانيه" ≈ 0.77) — مقيَّدة بمفاتيح ≥ 5 حروف لتفادي الكاذب
_STATE_FUZZY_CUTOFF = 0.75

# بادئات لهجية/نحوية شائعة قبل أسماء الأماكن: "للموصل"، "بالبصره"، "والحله"
_PLACE_PREFIXES = ("لل", "بال", "وال", "هال", "فال")


def _norm_key(name: str) -> str:
    """مفتاح مطابقة موحّد: نفس تطبيع فهرس اللهجة (بما فيه حذف «ال»)."""
    return " ".join(tokenize(name, keep_stopwords=True))


def _key_variants(key: str):
    """المفتاح نفسه + نسخة بلا بادئة مكانية شائعة إن وُجدت."""
    yield key
    for p in _PLACE_PREFIXES:
        if key.startswith(p) and len(key) - len(p) >= 3:
            yield key[len(p):]
            return


class LocationIndex:
    def __init__(self, path: str = LOCATIONS_PATH):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)

        self.states = {s["code"]: s["name"] for s in data["states"]}

        # اسم المحافظة قد يكون مركّباً ("الناصرية ذي قار") — نفهرس الاسم
        # الكامل وكل مقطع جزئي متتالٍ منه حتى تنطبق "ذي قار" أو "الناصرية"
        # أو "ميسان" لوحدها.
        self._state_lookup = {}
        for code, name in self.states.items():
            words = _norm_key(name).split()
            for n in range(len(words), 0, -1):
                for i in range(len(words) - n + 1):
                    part = " ".join(words[i:i + n])
                    if len(part) >= 3:
                        self._state_lookup.setdefault(part, code)

        # منطقة → المحافظات التي تحمل منطقة بهذا الاسم (قد يتكرر الاسم بأكثر
        # من محافظة؛ التصحيح الحتمي يشتغل فقط عند محافظة واحدة).
        self._district_lookup = defaultdict(dict)  # key -> {code: الاسم الأصلي}
        for d in data["districts"]:
            key = _norm_key(d["name"])
            if key:
                self._district_lookup[key][d["state"]] = d["name"]

        self._district_keys = list(self._district_lookup)
        self._state_keys = list(self._state_lookup)
        # للمطابقة الغيمية على المحافظات: المفاتيح القصيرة (< 5 حروف مثل
        # "حله"/"موصل") مستثناة — قريبة جداً من كلمات عادية
        self._state_fuzzy_keys = [k for k in self._state_keys if len(k) >= 5]
        # مفاتيح المناطق مجمّعة بعدد كلماتها — للمطابقة الغيمية بنفس الطول
        self._district_keys_by_len = defaultdict(list)
        for k in self._district_keys:
            self._district_keys_by_len[k.count(" ") + 1].append(k)

    # ------------------------------------------------------------------ #

    def canonical_state(self, city_text: str) -> Optional[dict]:
        """يرجع {code, name} للمحافظة المطابقة لنص المدينة، وإلا None."""
        key = _norm_key(city_text or "")
        if not key:
            return None
        code = None
        for variant in _key_variants(key):
            code = self._state_lookup.get(variant)
            if code:
                break
        if code is None and len(key) >= 5:
            close = difflib.get_close_matches(
                key, self._state_fuzzy_keys, n=1, cutoff=_STATE_FUZZY_CUTOFF
            )
            if close:
                code = self._state_lookup[close[0]]
        if code is None:
            return None
        return {"code": code, "name": self.states[code]}

    def state_for_district(self, district_text: str) -> Optional[dict]:
        """محافظة المنطقة إذا كانت المنطقة معروفة وتتبع محافظة واحدة فقط."""
        key = _norm_key(district_text or "")
        if not key:
            return None
        states = self._district_lookup.get(key)
        if states is None:
            n = key.count(" ") + 1
            close = difflib.get_close_matches(
                key, self._district_keys_by_len.get(n, []), n=1, cutoff=_FUZZY_CUTOFF
            )
            if close:
                states = self._district_lookup[close[0]]
        if states and len(states) == 1:
            code = next(iter(states))
            return {"code": code, "name": self.states[code], "district": states[code]}
        return None

    def search(self, text: str, top_k: int = 8) -> List[dict]:
        """يستخرج من نص حر كل أسماء المناطق/المحافظات الواردة فيه.

        كل نتيجة: {"district", "state_code", "state_name", "exact"} —
        district قد تكون None عند مطابقة اسم محافظة مباشرة. الأطول
        (الأكثر تحديداً) أولاً، والمطابقة الحرفية قبل الغيمية.
        """
        tokens = tokenize(text or "", keep_stopwords=True)
        if not tokens:
            return []

        # جولتان: المطابقة الحرفية أولاً على كامل النص (من الأطول للأقصر)
        # حتى لا تبتلعها مطابقة غيمية أوسع، ثم الغيمية على الكلمات المتبقية.
        # عند انطباق n-gram نستهلك كلماته حتى لا تنطبق "حي" من "حي الجمهوري"
        # مرة ثانية لوحدها.
        results, used = [], set()
        for fuzzy in (False, True):
            for n in range(min(_MAX_NGRAM, len(tokens)), 0, -1):
                for i in range(len(tokens) - n + 1):
                    span = range(i, i + n)
                    if used.intersection(span):
                        continue
                    key = " ".join(tokens[i:i + n])
                    # كلمة مفردة قصيرة: بدون مطابقة غيمية (إيجابيات كاذبة كثيرة)
                    if n == 1 and (len(key) < 3 or (fuzzy and len(key) < 5)):
                        continue

                    hit = self._match_exact(key) if not fuzzy else self._match_fuzzy(key, n)
                    if hit:
                        results.append(hit)
                        used.update(span)
                        if len(results) >= top_k:
                            return results
        return results

    def _district_hit(self, districts: dict, exact: bool) -> dict:
        # عند تكرار الاسم بأكثر من محافظة نرجع كل المرشحين بنتيجة واحدة
        code, name = next(iter(districts.items()))
        return {
            "district": name,
            "state_code": code,
            "state_name": self.states[code],
            "candidates": sorted(self.states[c] for c in districts),
            "exact": exact,
        }

    def _match_exact(self, key: str) -> Optional[dict]:
        for variant in _key_variants(key):
            districts = self._district_lookup.get(variant)
            if districts:
                return self._district_hit(districts, exact=True)
            state_code = self._state_lookup.get(variant)
            if state_code:
                return {
                    "district": None,
                    "state_code": state_code,
                    "state_name": self.states[state_code],
                    "candidates": [self.states[state_code]],
                    "exact": True,
                }
        return None

    def _match_fuzzy(self, key: str, n: int) -> Optional[dict]:
        # أسماء المحافظات أولاً (18 مفتاحاً، أوثق بكثير من 4900 منطقة —
        # "الدوانيه" لازم تنطبق على الديوانية قبل أي منطقة متشابهة)
        cutoff = _STATE_FUZZY_CUTOFF if n == 1 else _FUZZY_CUTOFF
        close = difflib.get_close_matches(key, self._state_fuzzy_keys, n=1, cutoff=cutoff)
        if close:
            code = self._state_lookup[close[0]]
            return {
                "district": None,
                "state_code": code,
                "state_name": self.states[code],
                "candidates": [self.states[code]],
                "exact": False,
            }
        # للمناطق: كلمة مفردة ما تدخل مطابقة غيمية (4900 اسم قصير → إيجابيات
        # كاذبة مثل "عبايه"→"العباسية")، والأسماء الأطول نشترط نفس عدد
        # الكلمات حتى لا يبتلع n-gram أوسع اسماً أقصر منه
        if n == 1:
            return None
        close = difflib.get_close_matches(
            key, self._district_keys_by_len.get(n, []), n=1, cutoff=_FUZZY_CUTOFF
        )
        if close:
            return self._district_hit(self._district_lookup[close[0]], exact=False)
        return None


_default_index: Optional[LocationIndex] = None


def _index() -> LocationIndex:
    global _default_index
    if _default_index is None:
        _default_index = LocationIndex()
    return _default_index


def search_locations(text: str, top_k: int = 8) -> List[dict]:
    return _index().search(text, top_k=top_k)


def all_state_names() -> List[str]:
    """الأسماء الرسمية للمحافظات الـ 18 كما بقاعدة بيانات شركة التوصيل —
    القيم الوحيدة المسموحة لحقل city بالمخرجات."""
    return sorted(_index().states.values())


def canonical_state(city_text: str) -> Optional[dict]:
    return _index().canonical_state(city_text)


def state_for_district(district_text: str) -> Optional[dict]:
    return _index().state_for_district(district_text)


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "ام مؤمل بغداد الحرية الثالثة بعد اسواق كلشي"
    print(f"النص: {q}\n")
    for r in search_locations(q):
        print(r)
