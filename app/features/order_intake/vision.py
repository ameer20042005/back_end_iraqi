# -*- coding: utf-8 -*-
"""وصف/استخراج نص من صورة (صورة طلب مكتوب بخط اليد، لقطة شاشة محادثة...).

مؤجَّل عمداً — القرار (تحميل Gemma4 عبر transformers خام لاستخدام قدرته
البصرية الأصلية، أو OCR مخصص أخف مثل Tesseract/EasyOCR) يحتاج معرفة حجم VRAM
المتوفر فعلياً على RunPod أولاً. الواجهة والمسار (router.py) جاهزان بالكامل؛
استبدل NotConfiguredImageDescriber بتطبيق حقيقي بسطر واحد هنا بدون أي تغيير
بمكان آخر.
"""

from abc import ABC, abstractmethod


class ImageDescriber(ABC):
    @abstractmethod
    async def describe(self, image_bytes: bytes) -> str:
        """يرجع نصاً عربياً يصف/يستخرج محتوى الصورة (مثلاً قائمة منتجات مطلوبة)."""


class NotConfiguredImageDescriber(ImageDescriber):
    async def describe(self, image_bytes: bytes) -> str:
        raise NotImplementedError(
            "ميزة وصف الصورة غير مفعّلة بعد — انظر TODO في app/features/order_intake/vision.py"
        )


image_describer: ImageDescriber = NotConfiguredImageDescriber()
