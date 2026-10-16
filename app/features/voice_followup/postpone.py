# -*- coding: utf-8 -*-
"""مواعيد تأجيل التسليم بمكالمة "صباح" — المصدر الحتمي الوحيد للتواريخ.

النموذج يفهم كلام الزبون ويختار **مفتاحاً** من جدول مغلق ("today" / "plus_N"
/ "weekday_D")، والتاريخ الفعلي يُحسب هنا بالكود وقت التثبيت فقط. خطأ التاريخ
له ثمن تشغيلي مباشر، فما ينحفظ بباك اند البيانات يبقى ناتج حساب حتمي لا نصاً
حراً من نموذج. أدوات الـ agent (agent.py) ترفض أي مفتاح خارج الجدول.
"""

from datetime import date, timedelta
from typing import Optional

# سقف التأجيل: يمنع قيماً عبثية ("أجلها سنة")، ويمنع خطأ نسخ صوتي برقم منطوق
# ("أربعين" بدل "أربعة") من تحويل مكالمة عادية لتأجيل مستحيل.
MAX_POSTPONE_DAYS = 14

# أسماء أيام الأسبوع بترقيم date.weekday() (الاثنين=0 … الأحد=6).
WEEKDAY_NAMES = {
    0: "الاثنين", 1: "الثلاثاء", 2: "الأربعاء", 3: "الخميس",
    4: "الجمعة", 5: "السبت", 6: "الأحد",
}


def _all_postpone_choices() -> "tuple[str, ...]":
    """مولَّد من السقف حتى يبقى رقماً واحداً يحكم المنظومة كلها."""
    return (
        ("today",)
        + tuple(f"plus_{d}" for d in range(1, MAX_POSTPONE_DAYS + 1))
        + tuple(f"weekday_{d}" for d in range(7))
    )


POSTPONE_CHOICES = _all_postpone_choices()


def postpone_days(choice: str, today: Optional[date] = None) -> int:
    """عدد الأيام من اليوم لموعد الخيار."""
    if choice == "today":
        return 0
    if choice.startswith("plus_"):
        return int(choice[len("plus_"):])
    if choice.startswith("weekday_"):
        today = today or date.today()
        target = int(choice[len("weekday_"):])
        delta = (target - today.weekday()) % 7
        # "خليها الخميس" بيوم خميس تعني الخميس الجاي، لا اليوم.
        return delta or 7
    raise ValueError(f"خيار تأجيل غير معروف: {choice!r}")


def resolve_postpone_date(choice: str, today: Optional[date] = None) -> str:
    """تاريخ ISO الفعلي لخيار تأجيل مؤكَّد."""
    today = today or date.today()
    return (today + timedelta(days=postpone_days(choice, today))).isoformat()


def option_label(choice: str, days: int) -> str:
    """التسمية اللي تنطقها صباح بالتأكيد — بنفس صيغة الزبون: من گال "الخميس"
    يسمع "يوم الخميس" مو "بعد 5 أيام"، حتى ما يشك إنها فهمته غلط."""
    if choice.startswith("weekday_"):
        return "يوم " + WEEKDAY_NAMES[int(choice[len("weekday_"):])]
    if days == 0:
        return "اليوم"
    if days == 1:
        return "باجر"
    if days == 2:
        return "بعد باجر"
    if days == 7:
        return "بعد أسبوع"
    if days == 14:
        return "بعد أسبوعين"
    return f"بعد {days} أيام"
