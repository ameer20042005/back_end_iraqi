# -*- coding: utf-8 -*-
"""صيغ مكالمة "راجع": معطيات الشحنة الراجعة كما يرسلها باك اند السستم.

الحقول هنا مشتقّة من CallRequest بخدمة الـ AI Call (انظر دليل التشغيل)،
لكن **بلا حقول المسار المؤجَّل** — هذي الحزمة تخص حالة "راجع" وحدها.

نفس فلسفة app/features/voice_followup/schema.py: ما نرسله لصباح هو فقط ما
وصل صراحة من مصدر موثوق (JSON الشحنة الأصلي)، لا نخترع ولا نستنتج. وكل
حقل اختياري يبقى Optional لأن غيابه حالة طبيعية متوقَّعة — البرومت
(prompts.py::_order_facts_lines) يحذف السطر كاملاً بدل ما يمرر "غير معروف"
للنموذج فيتوهم إنها معلومة ناقصة لازم يسأل عنها.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


# الشرح: عنصر واحد داخل الشحنة الراجعة. صباح تذكر الشحنة بالمنتج
# ("الغسالة اللي طلبتها") لا برقم الطلب — الزبون يتذكر المنتج، ما يتذكر
# "ORD-1042" — فهذا الكائن هو مصدر تلك الجملة. `quantity` افتراضها 1 لأن
# أغلب الشحنات قطعة وحدة، فما نجبر المرسِل يبعثها كل مرة.
class VoiceReturnItem(BaseModel):
    product_name: str
    quantity: int = 1


# الشرح: جسم طلب بدء مكالمة "راجع". قسّمنا الحقول لثلاث مجموعات حسب
# **مَن يشوفها**، لأن هذا الفرق جوهري بقواعد الخصوصية أدناه:
#
# 1) معرّفة داخلية (order_id) — لا تُنطق بالمكالمة إطلاقاً، تُستخدم فقط
#    بالإرسال لباك اند السستم عند حسم القرار.
# 2) تُذكَر بالافتتاح (status, store_name, amount/currency, items) — هذي
#    اللي تبني جملة "شحنتك من متجر كذا بمبلغ كذا، حالتها راجعة".
# 3) تُذكَر **فقط لو الزبون سأل** (tracking_number, goods_description) —
#    نفس قاعدة "قواعد الخصوصية" بالدليل: ممنوع ذكرها بالافتتاح.
#
# لاحظ غياب اسم الزبون وعنوانه ومحافظته عمداً: الدليل يمنع ذكرها نهائياً
# بالمكالمة، وما دامت صباح ما راح تنطقها فلا داعي نعرضها للنموذج أصلاً —
# تقليل سطح البيانات يقلّل احتمال التسريب بالهلوسة.
class VoiceReturnOrderRequest(BaseModel):
    order_id: str
    status: str = "راجع"
    store_name: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    items: List[VoiceReturnItem] = Field(default_factory=list)
    return_reason_hint: Optional[str] = None
    tracking_number: Optional[str] = None
    goods_description: Optional[str] = None
