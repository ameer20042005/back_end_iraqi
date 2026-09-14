# -*- coding: utf-8 -*-
"""إعدادات مشتركة (المحرك/RAG/الأدوات) — كلها قابلة للضبط عبر متغيرات بيئة.

تحذير أمني: لا تكتب أي قيمة سرّية (توكن/مفتاح) كافتراضي هنا مباشرة — هذا
الملف متتبَّع بـ git. كل الأسرار تُمرَّر فقط عبر متغيرات بيئة (`.env` محلياً
المستثنى بـ .gitignore، أو Environment Variables بإعدادات RunPod Pod).
"""

from dataclasses import dataclass
from typing import Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

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


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    # الموديل — النسخة المدموجة (base + LoRA اللهجة العراقية مندمجين بالأوزان
    # فعلياً عبر merge_and_unload، انظر gemma_iraqi_merge_fixed.ipynb) تشمل
    # أبراج الرؤية/الصوت كاملة. يخدمه خادم vLLM منفصل (انظر start.sh) —
    # القيمة هنا تُستخدم باسم الموديل بطلبات /v1/chat/completions ويقرأها
    # start.sh لتمريرها لـ `vllm serve`.
    # ⚠️ لازم يطابق MODEL_NAME بـ start.sh حرفياً — القيمتان مصدر واحد فقط
    # عملياً عبر متغير بيئة MODEL_NAME (تقرآه start.sh مباشرة، وتقرآه هنا
    # pydantic-settings تلقائياً)؛ الافتراضي هنا مجرد نسخة احتياطية لو ما
    # كان المتغير مضبوطاً — راح يفشل بـ404 "model does not exist" من vLLM
    # لو اختلف عن الاسم الفعلي اللي شُغِّل فيه خادم vLLM.
    model_name: str = "ameer4wisam/gemma-iraqi-10k-merged"

    # عنوان خادم vLLM OpenAI-متوافق — الباك اند عميل HTTP رفيع فقط (انظر
    # app/engine.py). محلياً بدون أي خادم يبقى ready=False وكل الميزات ترجع
    # لوضع fallback.
    # ⚠️ لازم يطابق VLLM_PORT بـ start.sh حرفياً — لا ربط تلقائي بينهما، فأي
    # تغيير بأحدهما يتطلب تحديث الآخر يدوياً.
    vllm_base_url: str = "http://127.0.0.1:18001/v1"

    # توكن Hugging Face — مطلوب لأن Gemma موديل بوابة (gated) وربما مستودع
    # الموديل خاص. لا قيمة افتراضية أبداً؛ يُقرأ فقط من متغير البيئة HF_TOKEN
    # (يقرأه start.sh ويمرره لخادم vLLM).
    hf_token: Optional[str] = None

    # إعدادات خادم vLLM (يقرأها start.sh ويمررها كأعلام لـ `vllm serve`):
    # نسبة VRAM المحجوزة للموديل + KV cache — 0.90 حسب الوصفة الرسمية.
    gpu_memory_utilization: float = 0.90
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
    # بمسارَي المبيعات والدعم — كان 4096 يترك ~1200 فقط وهو ضيّق لمحادثة طويلة.
    # الرفع فوق هذا يشتري سياقاً غير مستعمَل بثمن التزامن وكاش البادئة معاً —
    # لهذا اخترنا توجيه مساحة الـ VRAM الإضافية (مقارنة بـ A40) للتزامن
    # (MAX_NUM_SEQS) لا لطول السياق.
    max_model_len: int = 10000

    # طول الرد الأقصى — هذا **المقبض الوحيد الفعّال** لمرونة التوليد هنا.
    # تدرّج تاريخي: 64 (كانت تقصّ ردود المبيعات قسراً) → 150 → 256 الحالية.
    # 256 توكن ≈ 180-200 كلمة عربية، ويلزم منها الكثير عند **ملخّص الطلب**
    # (منتج + كمية + سعر + اسم + هاتف + عنوان بجملة وحدة، انظر
    # SALES_SYSTEM_PROMPT) — أطول رد بالمحادثة، وقصّه يقطع بيانات العميل.
    # زيادتها فوق 256 تزيد الإسهاب والكلفة بلا فائدة تُذكر لمحادثة مبيعات.
    max_new_tokens: int = 512

    # ⚠️ هذا الحقل **غير فعّال حالياً — يبقى للتوافق فقط**: app/engine.py
    # (_chat_completion) يكتب "temperature": 0.0 حرفياً بكل طلب ويتجاهل هذي
    # القيمة وأي temperature يجي بجسم الطلب. تغييرها هنا لا يغيّر أي سلوك.
    #
    # والتعطيل مقصود مو سهواً: الوصفة المعتمدة الوحيدة (خلية الاستدلال
    # الاحترافية بـ gemma_iraqi_merge_fixed.ipynb) حتمية بالكامل، وأي sampling
    # — حتى 0.3 — أنتج انهيار مخرجات كامل (هذيان غير مترابط) مع هذا الموديل
    # بالتجربة الفعلية على RunPod. يعني رفعها ما "يزيد المرونة"، يكسر الردود.
    # لو انبنى موديل مستقبلي يتحمّل sampling، الفتح يصير بـ engine.py مو هنا.
    temperature: float = 0.0

    # RAG (لهجة عراقية + مواقع) — استخراج الطلب (plane.md) وتصحيح الموقع
    # فقط؛ المبيعات والدعم يستدعيان بيانات المنتجات/الطلبات عبر أدوات
    # (app/tool_loop.py) لا حقناً تلقائياً.
    rag_top_k: int = 5

    # سقف عدد العناصر (منتجات أو طلبات) المسموح حقنها بالبرومبت دفعة وحدة —
    # انظر app/context_blocks.py::catalog_context_block/orders_context_block
    # وapp/tools/products.py وapp/features/support/router.py. الكتالوج/دفتر
    # الطلبات الكامل يتنافس على نفس ميزانية max_model_len مع البرومبت
    # وتاريخ المحادثة بكل دور (انظر تعليق max_model_len أعلاه) — حقن بلا حد
    # يفشل التوليد أو يبتر تاريخ المحادثة بصمت لو كبر الكتالوج/الدفتر الحقيقي.
    # ⚠️ رقم مبدئي متحفّظ — حجم الكتالوج/دفتر الطلبات الحقيقي غير معروف بعد
    # (نفس حالة TODOs الأخرى بالملف)؛ ارفعه بمجرد معرفة الحجم الفعلي وقياس
    # استهلاك التوكِن الناتج.
    max_injected_records: int = 80

    # باك اند السستم — النظام الداخلي اللي يملك بيانات المنتجات والطلبات
    # الحقيقية (انظر app/order_gateway.py وapp/products.py). هذا الباك اند
    # (الذكاء) ما يخزّن ولا منتج ولا طلب محلياً — كل استعلام (عبر أداة
    # search_products أو get_order_status) يمر لحظياً عبر HTTP لهذا العنوان
    # ولا يُحفظ. رابط ومسارات باك اند السستم الفعلية غير معروفة بعد؛ عدّل
    # القيمة هنا عند توفرها.
    system_backend_base_url: str = "http://127.0.0.1:9000"

    # مفاتيح حماية منفصلة لكل خدمة — مفتاح مختلف لكل ميزة، يُرسَل بهيدر
    # X-API-Key (انظر app/auth.py). فصلها عن بعض يسمح بإلغاء صلاحية خدمة
    # وحدها (مثلاً sales) بلا ما يأثر على الباقي.
    #
    # ⚠️ مكتوبة هنا كقيمة ثابتة بطلب صريح من صاحب المشروع (تفادياً لضبط .env
    # يدوياً بكل بيئة تشغيل) — خلافاً للتحذير الأمني بأعلى الملف. هذا الملف
    # متتبَّع بـ git، فأي شخص يصل للمستودع (بما فيه أي مستودع GitHub عام)
    # يرى هذي القيم حرفياً. .env (إذا وُجد) يبقى يتجاوزها كالعادة.
    sales_api_key: Optional[str] = "sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2"
    support_api_key: Optional[str] = "sk-support-7a9c2e4f6b1d8a0c3e5f7b9d1a3c5e7f"
    orders_api_key: Optional[str] = "sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c"
    voice_followup_api_key: Optional[str] = "sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b"

    # تحويل الصوت لنص (app/features/order_intake/transcribe.py) — موديل Whisper
    # مفرَّغ عليه اللهجة العربية (نموذج transformers عادي، يعمل بعملية FastAPI
    # نفسها — الصوت لا يمر بخادم vLLM)
    whisper_model_ar: str = Field(
        "ayoubkirouane/whisper-small-ar",
        validation_alias=AliasChoices("WHISPER_MODEL_AR", "WHISPER_MODEL"),
    )
    whisper_language_ar: str = Field("arabic", validation_alias="WHISPER_LANGUAGE_AR")

    # تحويل نص لصوت باللهجة العراقية (app/features/voice_followup/tts.py) —
    # يخدم مسار المتابعة الصوتية للطلبات (سؤال الزبون سبب الرفض/الإلغاء
    # صوتياً). موديل transformers منفصل عن Whisper، يعيش بعملية FastAPI نفسها.
    tts_model_ar: str = Field(
        "ameer4wisam/Habibi-TTS-IRQ",
        validation_alias=AliasChoices("TTS_MODEL_AR", "TTS_MODEL"),
    )
    tts_ckpt_ar: str = Field("Specialized/IRQ/model_100000.safetensors", validation_alias="TTS_CKPT_AR")
    tts_vocab_ar: str = Field("Specialized/IRQ/vocab.txt", validation_alias="TTS_VOCAB_AR")
    tts_ref_audio_ar: str = Field("reference/IRQ.wav", validation_alias="TTS_REF_AUDIO_AR")
    tts_ref_text_ar: str = Field(
        "اا ما نقدر ناخذ وقت أكثر، ااا لأنه شروط كلش يحتاجلها وقت.",
        validation_alias="TTS_REF_TEXT_AR",
    )

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
    whisper_model_ku: str = Field(
        "roshna-omer/whisper-small-Kurdish-Sorani", validation_alias="WHISPER_MODEL_KU",
    )

    # الشرح: مهمة التوليد للموديل الكردي. **ما نمرر language="arabic"** كما
    # بالعربي: الموديل مفرَّغ على الكردي، وفرض رمز لغة ثانية عليه يخرّب
    # مخرجه. None تعني "خلّي إعدادات التوليد المحفوظة بالموديل نفسه تقرر"
    # — وهي الصيغة الصحيحة لموديل مفرَّغ على لغة خارج قائمة Whisper.
    whisper_language_ku: Optional[str] = Field(None, validation_alias="WHISPER_LANGUAGE_KU")

    # الشرح: موديل TTS الكردي — F5-TTS مثل العربي بالضبط، من مبادرة
    # TTS4All. المستودع فيه ثلاثة أصوات (audiobook-female / audiobook-male
    # / studio-male). نختار **الأنثوي** لأن شخصية المتابعة الصوتية "صباح"
    # أنثى (انظر SABAH_SYSTEM_PROMPT)، وتبديل جنس الصوت بين العربي والكردي
    # يخلي نفس الموظفة تبدو شخصين مختلفين.
    #
    tts_model_ku: str = Field("aranemini/central-kurdish-tts", validation_alias="TTS_MODEL_KU")
    tts_ckpt_ku: str = Field("model-audiobook-female.pt", validation_alias="TTS_CKPT_KU")
    tts_vocab_ku: str = Field("vocab.txt", validation_alias="TTS_VOCAB_KU")

    # الشرح: F5-TTS يحتاج صوتاً مرجعياً + نصّه الحرفي لكل توليد. المستودع
    # الكردي يرفق زوجاً جاهزاً لكل صوت، فننزّلهما منه بدل ما نسجّل مرجعاً
    # بأنفسنا — خلافاً للعربي اللي مرجعه ملف محلي (reference/IRQ.wav).
    # النص ما نكتبه هنا: نقرأه من الملف المرافق وقت التحميل، لأن أي فرق
    # حرف واحد بينه وبين التسجيل يخرّب جودة الاستنساخ.
    #
    tts_ref_audio_ku: str = Field("prompt-audiobook-female.wav", validation_alias="TTS_REF_AUDIO_KU")
    tts_ref_text_ku: str = Field("prompt-audiobook-female.txt", validation_alias="TTS_REF_TEXT_KU")

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
