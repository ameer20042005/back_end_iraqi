# -*- coding: utf-8 -*-
"""وصف/استخراج نص من صورة عبر قدرة Gemma 4 البصرية الأصلية — بنفس خادم vLLM
المستخدَم بباقي الميزات (يدعم الصور عبر /v1/chat/completions بصيغة image_url؛
التحويل من PIL لـ data URI يصير بـ app/engine.py). **ماكو نسخة ثانية من
الموديل** — فقط استدعاء إضافي لنفس المحرك مع `multi_modal_data`.
"""

import io
from abc import ABC, abstractmethod

from app.engine import llm_engine

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


_DESCRIBE_PROMPT = (
    "هذي صورة طلب من زبون (ممكن تكون قائمة مكتوبة بخط اليد، لقطة شاشة محادثة، "
    "أو صورة منتج). اكتب وصفاً نصياً واضحاً بالعربي لكل شي يخص الطلب المذكور "
    "بالصورة: أسماء المنتجات، الكميات، وأي معلومات عن العميل إن وجدت. "
    "لا تكتب أي تحليل زائد، بس الوصف المباشر."
)


class ImageDescriber(ABC):
    @abstractmethod
    async def describe(self, image_bytes: bytes) -> str:
        """يرجع نصاً عربياً يصف/يستخرج محتوى الصورة (مثلاً قائمة منتجات مطلوبة)."""


class NotConfiguredImageDescriber(ImageDescriber):
    async def describe(self, image_bytes: bytes) -> str:
        raise NotImplementedError(
            "ميزة وصف الصورة غير متوفرة بهذه البيئة — Pillow غير مثبَّتة، أو محرك "
            "الموديل غير جاهز (طبيعي محلياً بدون GPU؛ يجب أن تعمل على RunPod)."
        )


class VllmVisionDescriber(ImageDescriber):
    """يستخدم نفس llm_engine (عميل vLLM) المستخدَم بباقي الميزات النصية —
    الصورة قبل النص بالبرومبت حسب توصية Gemma 4 لأفضل أداء."""

    async def describe(self, image_bytes: bytes) -> str:
        if not llm_engine.ready:
            raise NotImplementedError(
                "محرك الموديل غير جاهز (طبيعي محلياً بدون GPU؛ يجب أن يكون جاهزاً على RunPod)."
            )
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        messages = [{
            "role": "user",
            "content": [
                {"type": "image"},
                {"type": "text", "text": _DESCRIBE_PROMPT},
            ],
        }]
        prompt = llm_engine.render_multimodal_prompt(messages)
        response = await llm_engine.generate_full(
            prompt, max_tokens=512, temperature=0.3,
            multi_modal_data={"image": image},
        )
        return response.strip()


image_describer: ImageDescriber = (
    VllmVisionDescriber() if PIL_AVAILABLE else NotConfiguredImageDescriber()
)
