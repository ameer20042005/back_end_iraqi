# -*- coding: utf-8 -*-
"""درع أسماء المنتجات (app/guards.py: check_product_names, contradicts_availability).

الخلفية (عطل إنتاج كارثي، لقطة شاشة): الكتالوج فيه لابتوب واحد (لينوفو
IdeaPad 15). الزبون سأل «عندكم لابتوبات؟» فرد الوكيل:

    «ما عدنا لابتوبات حالياً، بس أگدر أعرضلك لابتوب Asus Core i7 رام 16
     اللي يوصلك خلال يومين»

ثلاث كوارث بجملة وحدة: نفى توفّر شي **عنده**، واخترع منتجاً ما نملكه
(Asus Core i7)، ووعد بموعد توصيل مخترع. حارس الأرقام مسك السعر المخترع فنقّحه،
لكن **اسم المنتج عبر بلا أي فحص** — فطلع رد يعرض منتجاً غير موجود.

الاختبارات هنا تحرس اتجاهين:
  ١. الماركة المخترعة تُمسك دائماً (وإلا نبيع ما ما نملك).
  ٢. الردود السليمة ما تنحجب — خصوصاً «ما عدنا X، بس عدنا Y» وY حقيقي، وهو
     بالضبط سلوك البائع الجيد.

التشغيل:  python -m pytest tests/test_product_name_guard.py -v
"""

import pytest

from app.guards import check_product_names, contradicts_availability

# كتالوج اختبار ثابت ومستقل عن أي مصدر بيانات حقيقي (app/products.py الآن
# يستعلم باك اند السستم لحظياً، بلا كتالوج محلي) — نفس المنتجات اللي كانت
# بـ app/data/products.json سابقاً، لأن الحالات أدناه مبنية عليها حرفياً.
_TEST_CATALOG = [
    {"name": "لابتوب لينوفو IdeaPad 15", "tags": ["لابتوب", "كمبيوتر", "لينوفو"],
     "description": "لابتوب خفيف للاستخدام اليومي، رام 8 جيجا، تخزين 512 SSD."},
    {"name": "حقيبة لابتوب مبطنة", "tags": ["حقيبة", "اكسسوار", "لابتوب"],
     "description": "حقيبة ظهر مبطنة تحمي اللابتوب لين 15.6 انج."},
    {"name": "ماوس لاسلكي لوجيتك", "tags": ["ماوس", "لاسلكي", "اكسسوار"],
     "description": "ماوس لاسلكي بطارية تدوم طويلاً، مناسب للمكتب والألعاب الخفيفة."},
    {"name": "سماعة بلوتوث JBL", "tags": ["سماعة", "بلوتوث", "صوت"],
     "description": "سماعة بلوتوث صوت نقي وبطارية تدوم 10 ساعات."},
]


def _full_catalog_text() -> str:
    return " ".join(
        f"{p['name']} {p.get('description', '')} {' '.join(p.get('tags', []))}"
        for p in _TEST_CATALOG
    )


_CATALOG = _full_catalog_text()


def _blocked(reply: str) -> bool:
    return bool(check_product_names(reply, _CATALOG)) or contradicts_availability(
        reply, _CATALOG
    )


# --------------------------------------------------------------------------
# ١. الاختراع يُمسك
# --------------------------------------------------------------------------

INVENTED_CASES = [
    (
        "لقطة الإنتاج حرفياً",
        "عذراً حبيبي، ما عدنا لابتوبات حالياً، بس أگدر أعرضلك لابتوب "
        "Asus Core i7 رام 16 اللي يوصلك خلال يومين",
    ),
    ("سعر منتج مخترع", "سعر لابتوب Asus Core i7 رام 16 هو 900,000 بعد الخصم"),
    ("ماركة لاتينية", "عدنا Dell Inspiron بسعر ممتاز"),
    ("ماركة عربية", "عدنا لابتوب ايسر جديد"),
    ("ماركة عربية ثانية", "عدنا سماعة سامسونك اصليه"),
    ("هاتف مخترع", "عدنا iPhone 15 برو"),
    ("نفي مع بديل مخترع", "ما عدنا لابتوبات، بس أعرضلك HP Pavilion"),
    ("معالج مخترع", "هذا اللابتوب بمعالج Ryzen 7"),
]


@pytest.mark.parametrize(
    "description,reply", INVENTED_CASES, ids=[c[0] for c in INVENTED_CASES]
)
def test_invented_product_is_caught(description, reply):
    assert _blocked(reply), f"{description}: منتج مخترع عبر الدرع"


def test_the_exact_production_bug():
    """العطل الأصلي: الماركة والطراز الاثنان يُمسكان."""
    reply = (
        "ما عدنا لابتوبات حالياً، بس أگدر أعرضلك لابتوب Asus Core i7 رام 16"
    )
    # الرموز ترجع بصيغتها الأصلية بالرد (مو موطَّأة) حتى تنقرأ باللوق كما كتبها
    # الموديل.
    caught = [t.lower() for t in check_product_names(reply, _CATALOG)]
    assert "asus" in caught
    assert "i7" in caught


# --------------------------------------------------------------------------
# ٢. الردود السليمة تمر
# --------------------------------------------------------------------------

CLEAN_CASES = [
    ("منتج الكتالوج", "عدنا لابتوب لينوفو IdeaPad 15 بـ750,000 دينار"),
    ("ماركة الكتالوج بالعربي", "لابتوب لينوفو خفيف وزين للاستخدام اليومي"),
    ("اكسسوار الكتالوج", "عدنا ماوس لوجيتك بـ15,000 دينار"),
    ("سماعة الكتالوج", "سماعة بلوتوث JBL بـ45,000 دينار"),
    ("نفي صادق بلا بديل", "عذراً حبيبي، ما عدنا تلفزيونات هسه"),
    ("نفي مع بديل حقيقي", "ما عدنا تلفزيونات، بس عدنا لابتوب لينوفو IdeaPad 15"),
    ("تحية", "هلا بيك حبيبي، شلون أگدر أساعدك؟"),
    ("سؤال عن الاسم", "أشگد أسمك الكريم حتى أسجّل الطلب باسمك؟"),
    ("ملخّص طلب", "طلبك: لابتوب لينوفو IdeaPad 15 عدد 1 بـ750,000 دينار. تأكدلي؟"),
]


@pytest.mark.parametrize(
    "description,reply", CLEAN_CASES, ids=[c[0] for c in CLEAN_CASES]
)
def test_clean_reply_is_not_blocked(description, reply):
    assert not _blocked(reply), f"{description}: إنذار كاذب حجب رداً سليماً"


def test_honest_denial_with_real_alternative_survives():
    """«ما عدنا X، بس عدنا Y» وY حقيقي = أفضل رد ممكن من بائع.

    بلا قيد «العرض مسنود بالكتالوج» كان الدرع يعاقب هذا الرد بالذات."""
    reply = "ما عدنا تلفزيونات هسه، بس عدنا لابتوب لينوفو IdeaPad 15 إذا يناسبك"
    assert not contradicts_availability(reply, _CATALOG)
    assert check_product_names(reply, _CATALOG) == []


# --------------------------------------------------------------------------
# ٣. الدرع مشتق من الكتالوج — يتكيّف بلا تعديل كود
# --------------------------------------------------------------------------


def _catalog_text(products):
    return " ".join(
        f"{p['name']} {p.get('description', '')} {' '.join(p.get('tags', []))}"
        for p in products
    )


def test_guard_adapts_when_a_product_is_added():
    """**الخاصية الجوهرية**: أضف منتجاً للكتالوج فيصير مسموحاً فوراً.

    الدرع ما عنده قائمة ماركات مكتوبة بالكود — كل شي مشتق من الكتالوج وقت
    التشغيل. لو انبدل مصدر المنتجات بقاعدة بيانات يشتغل كما هو."""
    base = [{
        "name": "لابتوب لينوفو IdeaPad 15",
        "description": "لابتوب خفيف، رام 8 جيجا.",
        "tags": ["لابتوب"],
    }]
    reply = "عدنا لابتوب Asus VivoBook 14 بمعالج Core i7"

    # قبل الإضافة: مخترع.
    assert check_product_names(reply, _catalog_text(base))

    # بعد الإضافة لنفس الكتالوج: مسموح، بلا أي تعديل كود.
    extended = base + [{
        "name": "لابتوب Asus VivoBook 14",
        "description": "لابتوب Asus بمعالج Core i7 ورام 16 جيجا.",
        "tags": ["لابتوب", "asus"],
    }]
    assert check_product_names(reply, _catalog_text(extended)) == []


def test_guard_adapts_for_arabic_brand_names():
    """نفس التكيّف للماركات المكتوبة بالعربي."""
    base = [{"name": "سماعة بلوتوث JBL", "description": "صوت نقي.", "tags": ["سماعة"]}]
    reply = "عدنا موبايل سامسونك Galaxy A54"
    assert check_product_names(reply, _catalog_text(base))

    extended = base + [{
        "name": "موبايل سامسونك Galaxy A54",
        "description": "موبايل سامسونك شاشة 6.4 انج.",
        "tags": ["موبايل", "سامسونك"],
    }]
    assert check_product_names(reply, _catalog_text(extended)) == []


def test_removed_product_becomes_forbidden_again():
    """والعكس: احذف منتجاً من الكتالوج فيصير ذكره اختراعاً."""
    with_product = [{
        "name": "لابتوب Asus VivoBook 14",
        "description": "لابتوب Asus.",
        "tags": ["لابتوب"],
    }]
    reply = "عدنا لابتوب Asus VivoBook 14"
    assert check_product_names(reply, _catalog_text(with_product)) == []
    assert check_product_names(reply, _catalog_text([]))
