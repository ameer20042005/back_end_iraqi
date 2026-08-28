# -*- coding: utf-8 -*-
"""مسار الصورة بـ /orders/create — استخراج الطلب من الصورة **باستدعاء واحد**.

سابقاً كان المسار خطوتين: (1) الموديل يصف الصورة بنص عربي حر، ثم (2) الموديل
النصي يستخرج JSON من ذلك الوصف. المشكلة إن أي معلومة ما يذكرها الوصف تضيع
نهائياً — أرقام الهاتف والأسعار كانت تسقط، والعروض المركّبة (منتج + منتج +
منتج بسعر واحد) تتفكّك لأصناف مكرّرة. وكلفة الاستدعاءين ضِعف زمن الاستجابة.

الآن نمرر الصورة مباشرة مع نفس برومبت plane.md وguided_json، فيخرج الـ JSON
من الصورة بخطوة واحدة: دقة أعلى وزمن أقل.

يحتاج Pillow + خادم vLLM جاهز (requirements-gpu.txt) — محلياً بدون GPU يرجع
الراوتر 501 واضحة.
"""

from abc import ABC, abstractmethod
from typing import List, Optional

from app.engine import llm_engine
from app.vision_utils import PIL_AVAILABLE, decode_image_bytes

_IMAGE_INSTRUCTION = (
    "هذي صورة طلب من زبون (قائمة مكتوبة بخط اليد، أو لقطة شاشة محادثة، أو صورة "
    "منتج). اقرأ كل النص الظاهر بالصورة — بضمنه أرقام الهواتف بالهيدر أو داخل "
    "الرسائل، والأسعار، والعناوين — وطبّق عليه نفس القواعد أعلاه حرفياً.\n"
    "ملاحظات خاصة بالصور:\n"
    "- انسخ أرقام الهواتف رقماً رقماً كما تظهر بالصورة، لا تخمّن أي خانة.\n"
    "- العرض المركّب (\"أ + ب + ج\" بسعر واحد) هو سطر واحد بـ orders باسمه "
    "الكامل كما ورد، مو عدة أصناف؛ والكمية المذكورة (\"3 قطع\") تخص العرض كله.\n"
    "- تجاهل عناصر الواجهة والزخارف: الوقت، أيقونات البطارية والشبكة، أزرار "
    "الاتصال، لوحة المفاتيح، علامات ✅ والقراءة.\n"
    "- عبارات البائع التسويقية أو التشغيلية (\"اضافة هدية\"، \"متوفر\"، "
    "\"وطريقة الاستعمال\") ليست منتجات — لا تدخلها بـ orders.\n"
    "أرجع JSON فقط."
)


class OrderImageReader(ABC):
    @abstractmethod
    async def extract(self, image_bytes: bytes, messages: List[dict], schema: dict) -> str:
        """يرجع JSON خام (نص) لطلب مستخرَج من الصورة، مقيَّداً بـ schema."""


class NotConfiguredImageReader(OrderImageReader):
    async def extract(self, image_bytes: bytes, messages: List[dict], schema: dict) -> str:
        raise NotImplementedError(
            "استخراج الطلب من الصورة غير متوفر بهذه البيئة — Pillow غير مثبَّتة، أو "
            "محرك الموديل غير جاهز (طبيعي محلياً بدون GPU؛ يجب أن يعمل على RunPod)."
        )


class VllmOrderImageReader(OrderImageReader):
    """يستخدم نفس llm_engine المستخدَم بباقي الميزات — الصورة قبل النص
    بالبرومبت حسب توصية Gemma 4 لأفضل أداء."""

    async def extract(self, image_bytes: bytes, messages: List[dict], schema: dict) -> str:
        if not llm_engine.ready:
            raise NotImplementedError(
                "محرك الموديل غير جاهز (طبيعي محلياً بدون GPU؛ يجب أن يكون جاهزاً على RunPod)."
            )
        image = decode_image_bytes(image_bytes)

        # نحقن الصورة برسالة المستخدم الأخيرة (اللي تحمل تعليمة الاستخراج
        # النهائية) — الصورة أولاً ثم النص.
        mm_messages = [dict(m) for m in messages]
        last = mm_messages[-1]
        last["content"] = [
            {"type": "image"},
            {"type": "text", "text": f"{last['content']}\n{_IMAGE_INSTRUCTION}"},
        ]

        return await llm_engine.generate_full(
            llm_engine.render_multimodal_prompt(mm_messages),
            max_tokens=384,
            temperature=0.0,
            guided_json=schema,
            multi_modal_data={"image": image},
        )


order_image_reader: OrderImageReader = (
    VllmOrderImageReader() if PIL_AVAILABLE else NotConfiguredImageReader()
)
