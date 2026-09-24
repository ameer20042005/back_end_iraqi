# -*- coding: utf-8 -*-
"""إعدادات التطبيق الثابتة.

هذا هو مصدر الإعدادات الوحيد للتطبيق ولـ ``start.sh``. لا تُقرأ ملفات ``.env``
ولا متغيرات بيئة النظام؛ غيّر القيم هنا مباشرة عند الحاجة.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

from app.lang import Lang


@dataclass(frozen=True)
class SpeechToTextModel:
    """نقطة تفتيش STT ومعطيات توليدها الخاصة باللغة."""

    repository: str
    language: Optional[str]


@dataclass(frozen=True)
class TextToSpeechModel:
    """كل ملفات موديل F5-TTS اللازمة للغة واحدة، من مصدر إعداد واحد."""

    repository: str
    checkpoint: str
    vocabulary: str
    reference_audio: str
    reference_text: str
    reference_assets_on_hub: bool


@dataclass(frozen=True)
class Settings:
    # الموديل — النسخة المدموجة (base + LoRA اللهجة العراقية مندمجين بالأوزان
    # فعلياً عبر merge_and_unload، انظر gemma_iraqi_merge_fixed.ipynb) تشمل
    # أبراج الرؤية/الصوت كاملة. يخدمه خادم vLLM منفصل (انظر start.sh) —
    # القيمة هنا تُستخدم باسم الموديل بطلبات /v1/chat/completions ويقرأها
    # start.sh لتمريرها لـ `vllm serve`.
    # start.sh يقرأ هذه القيمة مباشرة، لذلك لا يوجد مصدر ثانٍ قد يختلف عنها.
    model_name: str = "ameer4wisam/gemma-iraqi-10k-merged"

    # عنوان خادم vLLM OpenAI-متوافق — الباك اند عميل HTTP رفيع فقط (انظر
    # app/engine.py). محلياً بدون أي خادم يبقى ready=False وكل الميزات ترجع
    # لوضع fallback.
    # يطابق vllm_port أدناه؛ غيّرهما معاً عند نقل الخادم إلى منفذ آخر.
    vllm_base_url: str = "http://127.0.0.1:18001/v1"

    # توكن Hugging Face — مطلوب لأن Gemma موديل بوابة (gated) وربما مستودع
    # الموديل خاص. هذا استثناء الوحيد من مبدأ "كل الإعدادات هنا مباشرة": سرّ
    # حقيقي بهذا الملف يوقفه GitHub push protection ويكشفه لأي أحد بالريبو،
    # فيُقرأ فقط من متغير بيئة HF_TOKEN تضبطه بكل خادم عند النشر (RunPod
    # Environment Variables) — لا قيمة افتراضية حقيقية هنا أبداً.
    hf_token: Optional[str] = field(default_factory=lambda: os.environ.get("HF_TOKEN"))

    # إعدادات خادم vLLM (يقرأها start.sh ويمررها كأعلام لـ `vllm serve`):
    # نسبة VRAM المحجوزة للموديل + KV cache.
    gpu_memory_utilization: float = 0.85
    # طول السياق الأقصى — أقصر = مساحة KV cache أكبر = طلبات متزامنة أكثر.
    # ⚠️ لازم يطابق MAX_MODEL_LEN بـ start.sh (هو اللي يمرره فعلياً لـ vllm
    # serve؛ القيمة هنا للرجوع إليها بالكود فقط).
    #
    # المقايضة محسوبة على A100/H100 80GB+ بموديل 12B (كانت A40 48GB سابقاً —
    # انظر start.sh للحساب المحدَّث): الأوزان (bf16) تأخذ ~24.4GB من أصل ~72GB
    # متاحة (90% من 80GB)، فيتبقى للـ KV cache ~47.6GB. وKV عند 12B يكلّف
    # ~0.375MB لكل توكن (48 طبقة × 8 رؤوس KV × head_dim 256 × 2 لـ K و V ×
    # bf16)، يعني كل زيادة بالسياق تُضرب بعدد الطلبات المتزامنة:
    #     4096  = 1.5GB/طلب  → ~31 طلباً متزامناً (بالسياق الأقصى فعلياً)
    #     10000 = 3.7GB/طلب  → ~12 طلباً  ← الحالي (عملياً أكثر بكثير لأن أغلب
    #             المحادثات أقصر من الأقصى وتتقاسم صفحات الكاش — MAX_NUM_SEQS
    #             بـ start.sh مضبوط على 350 لهذا)
    #     20000 = 7.3GB/طلب  → ~6  طلبات
    #
    # ليش 10000 تحديداً: أطول برومبت عندنا (plane.md كاملاً ≈2552 توكن + كتل
    # RAG ≈362 + الرد 384) ≈3300 توكن، فالباقي (~6700) هامش لتاريخ المحادثة
    # بمسارات المحادثة — كان 4096 يترك ~1200 فقط وهو ضيّق لمحادثة طويلة.
    # الرفع فوق هذا يشتري سياقاً غير مستعمَل بثمن التزامن وكاش البادئة معاً —
    # لهذا اخترنا توجيه مساحة الـ VRAM الإضافية (مقارنة بـ A40) للتزامن
    # (MAX_NUM_SEQS) لا لطول السياق.
    max_model_len: int = 10000
    max_num_seqs: int = 90
    vllm_port: int = 18001
    api_port: int = 8000

    # طول الرد الأقصى — هذا **المقبض الوحيد الفعّال** لمرونة التوليد هنا.
    # تدرّج تاريخي: 64 (كانت تقصّ ردود المبيعات قسراً) → 150 → 256 الحالية.
    # 256 توكن ≈ 180-200 كلمة عربية، ويلزم منها الكثير عند **ملخّص الطلب**
    # (منتج + كمية + سعر + اسم + هاتف + عنوان بجملة وحدة، انظر
    # SALES_SYSTEM_PROMPT) — أطول رد بالمحادثة، وقصّه يقطع بيانات العميل.
    # زيادتها فوق 256 تزيد الإسهاب والكلفة بلا فائدة تُذكر لمحادثة مبيعات.
    max_new_tokens: int = 512

    # RAG (لهجة عراقية + مواقع) — استخراج الطلب (plane.md) وتصحيح الموقع
    # فقط؛ المبيعات تستدعي بيانات المنتجات عبر أدوات، لا حقناً تلقائياً.
    rag_top_k: int = 5

    # باك اند السستم — النظام الداخلي اللي يملك بيانات المنتجات والطلبات
    # الحقيقية (انظر app/order_gateway.py). هذا الباك اند
    # (الذكاء) ما يخزّن ولا منتج ولا طلب محلياً — استعلامات المنتجات
    # وإرسال الطلبات تمر لحظياً عبر HTTP لهذا العنوان
    # ولا يُحفظ. رابط ومسارات باك اند السستم الفعلية غير معروفة بعد؛ عدّل
    # القيمة هنا عند توفرها.
    system_backend_base_url: str = "http://127.0.0.1:8081/internal"

    # مفاتيح حماية منفصلة لكل خدمة — مفتاح مختلف لكل ميزة، يُرسَل بهيدر
    # X-API-Key (انظر app/auth.py). فصلها عن بعض يسمح بإلغاء صلاحية خدمة
    # وحدها (مثلاً sales) بلا ما يأثر على الباقي.
    #
    # هذه القيم ثابتة ولا يمكن تجاوزها بمتغيرات البيئة.
    sales_api_key: Optional[str] = "sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2"
    openai_compat_api_key: Optional[str] = "sk-openai-7a9c2e4f6b1d8a0c3e5f7b9d1a3c5e7f"
    orders_api_key: Optional[str] = "sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c"
    voice_followup_api_key: Optional[str] = "sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b"

    # تحويل الصوت لنص (app/features/order_intake/transcribe.py) — موديل Whisper
    # مفرَّغ عليه اللهجة العربية (نموذج transformers عادي، يعمل بعملية FastAPI
    # نفسها — الصوت لا يمر بخادم vLLM)
    whisper_model_ar: str = "ayoubkirouane/whisper-small-ar"
    whisper_language_ar: str = "arabic"

    # تحويل نص لصوت باللهجة العراقية (app/features/voice_followup/tts.py) —
    # يخدم مسار المتابعة الصوتية للطلبات (سؤال الزبون سبب الرفض/الإلغاء
    # صوتياً). موديل transformers منفصل عن Whisper، يعيش بعملية FastAPI نفسها.
    tts_model_ar: str = "ameer4wisam/Habibi-TTS-IRQ"
    tts_ckpt_ar: str = "Specialized/IRQ/model_100000.safetensors"
    tts_vocab_ar: str = "Specialized/IRQ/vocab.txt"
    tts_ref_audio_ar: str = "reference/IRQ.wav"
    tts_ref_text_ar: str = "اا ما نقدر ناخذ وقت أكثر، ااا لأنه شروط كلش يحتاجلها وقت."

    # -----------------------------------------------------------------
    # الكردية السورانية (ckb)
    # -----------------------------------------------------------------
    #
    # الشرح: القرار المعماري هنا أن الكردية **ما تحتاج كود جديد** — تحتاج
    # نقاط تفتيش (checkpoints) ثانية بنفس الواجهتين الموجودتين:
    #   · STT: موديل transformers عادي، نفس pipeline("automatic-speech-
    #     recognition") المستعمل بالعربي (transcribe.py).
    #   · TTS: موديل F5-TTS، نفس واجهة F5TTS(ckpt_file=..., vocab_file=...)
    #     المستعملة بالعربي (voice_followup/tts.py) — بما فيها حاجته لصوت
    #     مرجعي ونصّه الحرفي (zero-shot voice cloning).
    # لذلك التفعيل = تعبئة هذي القيم + توجيه حسب اللغة، لا مسار مستقل.

    # الشرح: Whisper الأصلي **ما يدعم الكردية إطلاقاً** — لغاته الـ99 ماكو
    # بيها ku ولا ckb، وموديلنا العربي الحالي مفرَّغ على العربية وحدها.
    # فلازم نقطة تفتيش مفرَّغة على السوراني. القيمة أدناه مفرَّغة من
    # openai/whisper-small (نفس حجم موديلنا العربي) بمعدل خطأ كلمات ~24%.
    #
    whisper_model_ku: str = "roshna-omer/whisper-small-Kurdish-Sorani"

    # الشرح: مهمة التوليد للموديل الكردي. **ما نمرر language="arabic"** كما
    # بالعربي: الموديل مفرَّغ على الكردي، وفرض رمز لغة ثانية عليه يخرّب
    # مخرجه. None تعني "خلّي إعدادات التوليد المحفوظة بالموديل نفسه تقرر"
    # — وهي الصيغة الصحيحة لموديل مفرَّغ على لغة خارج قائمة Whisper.
    whisper_language_ku: Optional[str] = None

    # الشرح: موديل TTS الكردي — F5-TTS مثل العربي بالضبط، من مبادرة
    # TTS4All. المستودع فيه ثلاثة أصوات (audiobook-female / audiobook-male
    # / studio-male). نختار **الأنثوي** لأن شخصية المتابعة الصوتية "صباح"
    # أنثى (انظر SABAH_SYSTEM_PROMPT)، وتبديل جنس الصوت بين العربي والكردي
    # يخلي نفس الموظفة تبدو شخصين مختلفين.
    #
    tts_model_ku: str = "aranemini/central-kurdish-tts"
    tts_ckpt_ku: str = "model-audiobook-female.pt"
    tts_vocab_ku: str = "vocab.txt"

    # الشرح: F5-TTS يحتاج صوتاً مرجعياً + نصّه الحرفي لكل توليد. المستودع
    # الكردي يرفق زوجاً جاهزاً لكل صوت، فننزّلهما منه بدل ما نسجّل مرجعاً
    # بأنفسنا — خلافاً للعربي اللي مرجعه ملف محلي (reference/IRQ.wav).
    # النص ما نكتبه هنا: نقرأه من الملف المرافق وقت التحميل، لأن أي فرق
    # حرف واحد بينه وبين التسجيل يخرّب جودة الاستنساخ.
    #
    tts_ref_audio_ku: str = "prompt-audiobook-female.wav"
    tts_ref_text_ku: str = "prompt-audiobook-female.txt"

    # الشرح: تحميل كسول — الموديلات الكردية **ما تنحمّل عند الإقلاع**
    # (خلافاً للعربية، انظر warmup بـmain.py). السبب تشغيلي موثّق بالكود
    # نفسه: vLLM يحجز أغلب VRAM على A40، وتحميل موديلين إضافيين دائماً
    # يخاطر بـOOM يسقط المسار العربي (الأكثر استعمالاً) عشان مسار أقل
    # استعمالاً. يُحمّل الموديل أول ما يوصل زبون كردي فعلاً، ويُفرَّغ بعد
    # خمول بهذي المدة لإرجاع الذاكرة. صفر = لا تفريغ أبداً.
    #
    ku_model_idle_unload_seconds: int = 900

    def stt_model_for(self, language: Lang) -> SpeechToTextModel:
        """اختيار STT المركزي؛ ما تبقى شروط موديلات موزعة بالراوترات."""
        if language is Lang.KU:
            return SpeechToTextModel(self.whisper_model_ku, self.whisper_language_ku)
        return SpeechToTextModel(self.whisper_model_ar, self.whisper_language_ar)

    def tts_model_for(self, language: Lang) -> TextToSpeechModel:
        """اختيار TTS المركزي، شاملاً كل ملفات نقطة التفتيش والمرجع."""
        if language is Lang.KU:
            return TextToSpeechModel(
                self.tts_model_ku,
                self.tts_ckpt_ku,
                self.tts_vocab_ku,
                self.tts_ref_audio_ku,
                self.tts_ref_text_ku,
                True,
            )
        return TextToSpeechModel(
            self.tts_model_ar,
            self.tts_ckpt_ar,
            self.tts_vocab_ar,
            self.tts_ref_audio_ar,
            self.tts_ref_text_ar,
            False,
        )


settings = Settings()
