# -*- coding: utf-8 -*-
"""أدوات صور مشتركة — يستوردها أي مسار يرسل صورة للموديل البصري (Gemma 4
عبر vLLM) بدل تكرار منطق فك الترميز/التصغير بكل ميزة على حدة.

انتُزعت من app/features/order_intake/vision.py (كانت أول استعمال — استخراج
طلب من صورة) لأن app/features/sales/router.py احتاج نفس المنطق بالضبط
(next.md: تحليل صورة منتج بمحادثة مبيعات لمطابقتها مع الكتالوج)."""

import io

try:
    from PIL import Image

    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# أقصى بُعد للصورة قبل الإرسال. لقطات شاشة الهواتف تجي بعرض 1080-1440px،
# وتصغيرها لـ 896 يقلّل توكنات الرؤية وزمن الترميز محسوساً بدون ما يضر
# قراءة النص العربي بلقطات المحادثات (النص يبقى واضحاً فوق ~700px).
MAX_IMAGE_DIM = 896


def downscale_image(image: "Image.Image") -> "Image.Image":
    """يصغّر الصورة لأقصى بُعد MAX_IMAGE_DIM مع حفظ النسبة — أهم مكسب سرعة
    بمسار الصور: توكنات الرؤية تتناسب مع مساحة الصورة.

    `reducing_gap` يخلي Pillow يسوي تصغيراً تقريبياً سريعاً أولاً (draft) ثم
    LANCZOS على النتيجة الأصغر، بدل تمرير LANCZOS على الأصل كاملاً. صورة
    12 ميجابكسل من كاميرا الهاتف كانت تكلّف مئات الميلي-ثانية بالتصغير وحده."""
    w, h = image.size
    longest = max(w, h)
    if longest <= MAX_IMAGE_DIM:
        return image
    scale = MAX_IMAGE_DIM / longest
    return image.resize(
        (int(w * scale), int(h * scale)), Image.LANCZOS, reducing_gap=2.0
    )


def decode_image_bytes(image_bytes: bytes) -> "Image.Image":
    """يفكّ بايتات صورة خام لصورة PIL جاهزة للإرسال للموديل — RGB ومصغَّرة.

    `draft` يخلي فاكّ ترميز JPEG نفسه يقرأ الصورة بدقة أقل مباشرة (1/2، 1/4،
    1/8) بدل فكّها كاملة ثم تصغيرها — أرخص مرحلة نقدر نحذفها بمسار الصور،
    وصور الهواتف كلها JPEG عملياً. لا أثر على الصيغ الأخرى (Pillow يتجاهل
    draft لو الصيغة ما تدعمه)."""
    raw = Image.open(io.BytesIO(image_bytes))
    raw.draft("RGB", (MAX_IMAGE_DIM, MAX_IMAGE_DIM))
    return downscale_image(raw.convert("RGB"))
