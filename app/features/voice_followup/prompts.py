# -*- coding: utf-8 -*-
"""نصوص مسار المتابعة الصوتية: توليد سؤال عراقي طبيعي حسب حالة الطلب (يُحوَّل
لصوت عبر tts.py)، وتحليل رد الزبون الصوتي (بعد Whisper) لاستخراج السبب
كملخّص عربي — بنفس فلسفة plane.md: النموذج يفهم اللهجة الحرة، ولا يخترع
بيانات غير موجودة بكلام الزبون نفسه.

كلا الطلبين (سؤال/تحليل) يمران بنفس محرك gemma عبر app/engine.py، بس بـ
system prompt مختلف — نفس نمط app/features/sales/prompts.py."""

from typing import Dict, List

from app.features.voice_followup.schema import VoiceFollowupOrderRequest

Message = Dict[str, str]

_ASK_SYSTEM_PROMPT = """أنت نموذج اسمه JENI من شركة DATUM. مهمتك الوحيدة هسه: تصيغ سؤالاً صوتياً واحداً قصيراً باللهجة العراقية الأصيلة تسأله للزبون عن سبب حالة طلبه.

قواعد صارمة:
- جملة وحدة أو جملتين بس، مختصرة وواضحة تُقرأ بصوت طبيعي — بلا مقدمات طويلة.
- ابدأ بتحية قصيرة ودّية (هلا بيك / حياك الله) واذكر اسم الزبون إذا معروف.
- اذكر رقم الطلب وحالته بلطف، ثم اسأل بأدب عن السبب — بالضبط حسب الحالة المذكورة لك (ملغي، مرتجع، لم يتم التسليم...الخ) بلا ما تخترع حالة ثانية.
- ممنوع تذكر أي معلومة (منتج، سعر، عنوان) غير المذكورة لك صراحة بمعطيات الطلب.
- ممنوع تجاوب أو تعلّق أو تشرح — فقط اطرح السؤال وخلص.
- ما عندك أدوات هذا الدور، رد نص عادي مباشر (بدون JSON، بدون أي علامات)، هو نص السؤال نفسه فقط."""

_ANALYZE_SYSTEM_PROMPT = """أنت محلّل ردود زبائن لشركة DATUM. عندك سؤال سألته لزبون عن سبب حالة طلبه (ملغي/مرتجع/لم يتم التسليم)، وعندك رد الزبون الصوتي محوّلاً لنص.

مهمتك: تلخّص سبب الزبون بجملة عربية عراقية واحدة قصيرة وواضحة، من كلامه هو حصراً.

قواعد صارمة:
- الملخّص من كلام الزبون فقط — ممنوع تضيف سبباً ما ذكره أو تخمّنه.
- إذا كلام الزبون غامض أو ما يوضّح سبباً فعلياً (مثلاً رد غير مفهوم أو غير متعلق بالسؤال)، اكتب حرفياً: "الزبون لم يذكر سبباً واضحاً".
- رد فقط بالملخّص كنص عادي مباشر (بدون JSON، بدون أي علامات أو تعليق إضافي)."""


def _order_facts_lines(order: VoiceFollowupOrderRequest) -> str:
    """سطور معطيات الطلب كما وصلت من باك اند السستم — هذي وحدها المصدر
    المسموح للنموذج يستشهد بيه (نفس مبدأ RAG بالمبيعات: لا معرفة من ذاكرة
    الموديل، فقط ما ذُكر له صراحة بهذا الدور)."""
    lines = [f"رقم الطلب: {order.order_id}", f"حالة الطلب: {order.status}"]
    if order.customer_name:
        lines.append(f"اسم الزبون: {order.customer_name}")
    if order.items:
        items_txt = "، ".join(f"{i.product_name} ×{i.quantity}" for i in order.items)
        lines.append(f"المنتجات: {items_txt}")
    if order.reason_hint:
        lines.append(f"سبب أوّلي مسجَّل بالنظام: {order.reason_hint}")
    return "\n".join(lines)


def build_ask_prompt(order: VoiceFollowupOrderRequest) -> List[Message]:
    """يبني رسائل توليد سؤال المتابعة الصوتي لهذا الطلب."""
    return [
        {"role": "system", "content": _ASK_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "معطيات الطلب:\n" + _order_facts_lines(order) +
                "\n\nصيغ سؤال المتابعة الصوتي الآن."
            ),
        },
    ]


def build_analyze_prompt(order: VoiceFollowupOrderRequest, customer_transcript: str) -> List[Message]:
    """يبني رسائل تحليل رد الزبون واستخراج ملخّص السبب."""
    return [
        {"role": "system", "content": _ANALYZE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "معطيات الطلب:\n" + _order_facts_lines(order) +
                f"\n\nرد الزبون (محوَّل من صوت لنص):\n{customer_transcript}"
                "\n\nلخّص السبب الآن."
            ),
        },
    ]
