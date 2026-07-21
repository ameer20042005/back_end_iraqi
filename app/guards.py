# -*- coding: utf-8 -*-
"""دروع ثنائية الاتجاه — نفس فلسفة خلية الاستدلال النهائية بـ
gemma_iraqi_merge_fixed.ipynb (v2.2)، مُطبَّقة هنا على كتالوج ديناميكي (RAG)
بدل كتالوج ثابت:

  1) check_numbers  — درع الأرقام: أي رقم مالي برد الموديل لازم يطابق حرفياً
     نص مرجعي (سياق منتجات RAG). كان موجوداً هنا أصلاً وهو مربوط فعلياً
     بـ app/features/sales/router.py (يستبدل الرد، مو تسجيل فقط).

  2) check_topics   — درع المواضيع: يُبنى من نفس نص الكتالوج وقت التشغيل.
     الموضوع (ضمان/تركيب/توصيل/تقسيط...) موجود بنص المنتجات المسترجَعة؟
     الرد مسموح. غير موجود؟ الرد لازم يحيل ("أتأكدلك") وإلا يُعتبر هلوسة.
     بما إن الكتالوج هنا ديناميكي (كل استعلام يرجّع منتجات مختلفة عبر RAG،
     مو نص ثابت بالبرومت)، الدرع يتكيف تلقائياً بدون أي تعديل كود مع أي
     منتج يُضاف لـ app/data/products.json أو يُستبدل بمصدر بيانات حقيقي.

الاستخدام (انظر app/features/sales/router.py):
    from app.guards import check_numbers, check_topics
    bad_numbers = check_numbers(answer, reference_text)
    reason = check_topics(answer, user_message, reference_text)
"""

import re
from typing import List, Optional

_NUMBER_RE = re.compile(r"\d[\d,\.]*")
_PHONE_RE = re.compile(r"^07\d{9}$")


def _strip_separators(num: str) -> str:
    """يزيل فواصل الآلاف (750,000 → 750000) حتى تتطابق مع أرقام الكتالوج
    الخام (بدون تنسيق) رغم اختلاف صيغة الكتابة."""
    return num.replace(",", "")


def check_numbers(reply: str, reference_text: str) -> List[str]:
    """يرجع قائمة الأرقام المالية بـ `reply` غير الموجودة حرفياً بـ
    `reference_text`. يتجاهل أرقاماً قصيرة بدون فواصل/عشرية (≤ رقمين) لأنها
    غالباً عدد قطع أو سنين ضمان، مو سعراً، وأرقام هاتف عراقية (07xxxxxxxxx)
    لأنها بيانات عميل مو سعراً مختلَقاً."""
    allowed = {_strip_separators(n) for n in _NUMBER_RE.findall(reference_text)}
    bad = []
    for num in _NUMBER_RE.findall(reply):
        clean = num.rstrip(".,")
        if _PHONE_RE.match(clean):
            continue
        norm = _strip_separators(clean)
        if norm in allowed:
            continue
        if "," not in clean and "." not in clean and len(clean) <= 2:
            continue
        bad.append(clean)
    return bad


# ============================================================
# درع المواضيع — الاتجاه الثاني (منع هلوسة مواضيع غير مغطّاة بالكتالوج)
# ============================================================

# مجموعات المواضيع الحساسة: (اسم، كلمات السؤال/الرد المطابقة)
TOPIC_GROUPS = {
    "ضمان": ["ضمان", "كفالة", "گارنتي", "وارنتي", "الكفالة"],
    "توصيل": ["توصيل", "شحن", "يوصلون"],
    "تركيب": ["تركيب", "نصب", "التنصيب"],
    "تقسيط": ["تقسيط", "اقساط", "أقساط", "قسط"],
    "صيانة": ["صيانة", "تصليح", "قطع غيار"],
    "لون": ["لون", "الوان", "ألوان"],
}

# صيغ الإحالة الشرعية — رد يحتوي إحداها يُسمح حتى بموضوع غير مغطّى بالكتالوج
CONFIRM_PHRASES = [
    "أتأكدلك", "اتأكدلك", "أتأكد لك", "أسأل وأرد", "اسأل وأرد",
    "ما أگدر أجزم", "ما اگدر اجزم", "أتحقق",
]
SAFE_REPLY = "خليني أتأكد من هذي المعلومة وأرد عليك، حتى ما أگلك شي غلط."

_ARABIC_PREFIX = re.compile(r"^(?:وال|بال|لل|فال|ال|و|ب|ف)")
_ARABIC_PUNCT = re.compile(r"[،؛؟ـ]")


def _word_set(text: str) -> set:
    """كلمات عربية مطابقة على مستوى الكلمة (محصّن ضد التصاق الترقيم
    والبادئات — نفس منطق _word_set بخلية الاستدلال بالنوتبوك)."""
    words = re.findall(r"[؀-ۿ]+", _ARABIC_PUNCT.sub(" ", text))
    return {_ARABIC_PREFIX.sub("", w) for w in words} | set(words)


def check_topics(reply: str, user_message: str, reference_text: str) -> Optional[str]:
    """يرجع سبب التدخل (str) إذا الرد يتطرق لموضوع حساس (ضمان/تركيب/تقسيط...)
    مو موجود بـ `reference_text` (سياق منتجات RAG المسترجَعة لهذا السؤال)
    وبدون صيغة إحالة صريحة. يرجع None إذا الرد آمن (الموضوع مغطّى بالكتالوج،
    أو الرد أصلاً يحيل بدل ما يدّعي معلومة).

    ملاحظة: الكتالوج هنا نص RAG ديناميكي (وصف/تاگات منتج فعلي)، مو قائمة
    مواضيع ثابتة — فلو انضاف حقل "ضمان" لوصف منتج بـ products.json، الدرع
    يرخّص موضوع الضمان تلقائياً بدون أي تعديل هنا."""
    reply_words = _word_set(reply)
    user_words = _word_set(user_message)
    catalog_words = _word_set(reference_text)

    for name, kws in TOPIC_GROUPS.items():
        asked = any(k in user_words for k in kws)
        mentioned = any(k in reply_words for k in kws)
        if not (asked or mentioned):
            continue
        covered = any(k in catalog_words or k in reference_text for k in kws)
        if covered:
            continue
        if any(p in reply for p in CONFIRM_PHRASES):
            continue
        return f"'{name}' غير مغطّى بمعلومات المنتج المسترجَعة — منع هلوسة"
    return None
