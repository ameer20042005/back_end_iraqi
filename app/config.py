# -*- coding: utf-8 -*-
"""إعدادات التطبيق الثابتة.

هذا هو مصدر إعدادات التطبيق و``start.sh``. تُقرأ الأسرار ومتغيرات ميزة
تصحيح المناطق من البيئة؛ بقية القيم تُضبط هنا مباشرة.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class SpeechToTextModel:
    """نقطة تفتيش النسخ العربي ومعطيات توليدها."""

    repository: str
    language: Optional[str]


@dataclass(frozen=True)
class TextToSpeechModel:
    """ملفات موديل النطق العراقي ومرجعه، من مصدر إعداد واحد."""

    repository: str
    checkpoint: str
    vocabulary: str
    reference_audio: str
    reference_text: str


@dataclass(frozen=True)
class Settings:
    # الموديل — النسخة المدموجة (base + LoRA اللهجة العراقية مندمجين بالأوزان
    # فعلياً عبر merge_and_unload، انظر gemma_iraqi_merge_fixed.ipynb) تشمل
    # أبراج الرؤية/الصوت كاملة. يخدمه خادم vLLM منفصل (انظر start.sh) —
    # القيمة هنا تُستخدم باسم الموديل بطلبات /v1/chat/completions ويقرأها
    # start.sh لتمريرها لـ `vllm serve`.
    # start.sh يقرأ هذه القيمة مباشرة، لذلك لا يوجد مصدر ثانٍ قد يختلف عنها.
    #
    # التشغيل المحلي (start_local.sh) يتجاوز الاسم والرابط بمتغيرَي البيئة
    # LLM_MODEL وLLM_BASE_URL (افتراضياً http://localhost:1234/v1). start.sh ما
    # يضبطهما أبداً، فتبقى القيم الثابتة على RunPod. `or` بدل قيمة getenv
    # الافتراضية حتى ما تصير القيمة فارغة لو صُدِّر المتغير فارغاً.
    model_name: str = field(default_factory=lambda: os.getenv("LLM_MODEL") or "google/gemma-4-E4B-it")

    # عنوان خادم vLLM OpenAI-متوافق — الباك اند عميل HTTP رفيع فقط (انظر
    # app/engine.py). محلياً بدون أي خادم يبقى ready=False وكل الميزات ترجع
    # لوضع fallback.
    # يطابق vllm_port أدناه؛ غيّرهما معاً عند نقل الخادم إلى منفذ آخر.
    vllm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL") or "http://127.0.0.1:18001/v1")

    # مهلة طلب توليد واحد بالثواني. الموديلات المحلية مع التفكير قد تأخذ
    # دقيقة؛ تجاوزها يرجع AI_REQUEST_TIMEOUT (504) بدل تعليق الطلب.
    llm_request_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS") or "120"))

    # دعم الصور بالموديل المحمَّل: auto = يُسأل LM Studio عن نوع الموديل
    # (vlm)، وإن تعذّر (vLLM) يُفترض الدعم. true/false يفرضان القيمة.
    # الصورة لا تُرسَل للموديل إلا إذا كانت النتيجة دعماً فعلياً.
    llm_vision: str = field(default_factory=lambda: (os.getenv("LLM_VISION") or "auto").strip().lower())

    # لا يوجد توكن Hugging Face: كل مستودعات الموديلات هنا (Gemma المدموج،
    # Whisper، F5-TTS) عامة وغير gated، فتُنزَّل بلا مصادقة (انظر start.sh).

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
    max_model_len: int = 32768
    max_num_seqs: int = 90
    # يقابل Evaluation Batch Size في LM Studio؛ vLLM يستخدمه كحد أقصى
    # لعدد التوكنات المجدولة في الدفعة الواحدة.
    max_num_batched_tokens: int = 2048
    vllm_port: int = 18001
    api_port: int = 8000

    # طول الرد الأقصى — هذا **المقبض الوحيد الفعّال** لمرونة التوليد هنا.
    # تدرّج تاريخي: 64 (كانت تقصّ ردود المبيعات قسراً) → 150 → 256 الحالية.
    # 256 توكن ≈ 180-200 كلمة عربية، ويلزم منها الكثير عند **ملخّص الطلب**
    # (منتج + كمية + سعر + اسم + هاتف + عنوان بجملة وحدة، انظر
    # SALES_SYSTEM_PROMPT) — أطول رد بالمحادثة، وقصّه يقطع بيانات العميل.
    # زيادتها فوق 256 تزيد الإسهاب والكلفة بلا فائدة تُذكر لمحادثة مبيعات.
    max_new_tokens: int = 8192

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
    district_api_key: Optional[str] = "sk-district-7ea5de0fb1b68f0283df460dea42b2df"

    # خدمة تصحيح مناطق شركات التوصيل: نفس FastAPI مع مفتاحها المستقل أعلاه.
    # مسارات الكتالوج وإعدادات LLM الاختيارية تُقرأ من البيئة.
    district_source_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "DISTRICT_SOURCE_DIR", str(Path(__file__).resolve().parent.parent / "assets" / "address"))))
    district_database_path: Path = field(default_factory=lambda: Path(os.getenv(
        "DISTRICT_DATABASE_PATH", str(Path(__file__).resolve().parent / "features" / "district_correction" / "data" / "catalog.sqlite3"))))
    district_llm_base_url: str = field(default_factory=lambda: os.getenv("DISTRICT_LLM_BASE_URL", ""))
    district_llm_api_key: str = field(default_factory=lambda: os.getenv("DISTRICT_LLM_API_KEY", ""))
    district_llm_model: str = field(default_factory=lambda: os.getenv("DISTRICT_LLM_MODEL", ""))
    district_llm_max_cases: int = field(default_factory=lambda: int(os.getenv("DISTRICT_LLM_MAX_CASES", "1000")))
    district_llm_concurrency: int = field(default_factory=lambda: max(1, int(os.getenv("DISTRICT_LLM_CONCURRENCY", "4"))))
    district_llm_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("DISTRICT_LLM_TIMEOUT_SECONDS", "60")))
    # ذاكرة التصحيحات (تأكيد المراجع + التعلّم التلقائي): قاعدة منفصلة عن
    # الكتالوج حتى لا تُمسح عند إعادة استيراد ملفات Excel.
    district_alias_database_path: Path = field(default_factory=lambda: Path(os.getenv(
        "DISTRICT_ALIAS_DATABASE_PATH", str(Path(__file__).resolve().parent / "features" / "district_correction" / "data" / "aliases.sqlite3"))))
    # حفظ النتيجة تلقائياً عندما تتفق طريقتان مستقلتان (LLM + القواعد أو البحث الدلالي).
    district_auto_learn: bool = field(default_factory=lambda: os.getenv("DISTRICT_AUTO_LEARN", "true").lower() not in ("0", "false", "no"))
    # بحث دلالي اختياري عبر خادم Embeddings متوافق مع OpenAI (vLLM أو TEI).
    # فارغ = معطّل. موديلات e5 تحتاج البادئتين "query: " و"passage: ".
    district_embedding_base_url: str = field(default_factory=lambda: os.getenv("DISTRICT_EMBEDDING_BASE_URL", ""))
    district_embedding_api_key: str = field(default_factory=lambda: os.getenv("DISTRICT_EMBEDDING_API_KEY", ""))
    district_embedding_model: str = field(default_factory=lambda: os.getenv("DISTRICT_EMBEDDING_MODEL", ""))
    district_embedding_query_prefix: str = field(default_factory=lambda: os.getenv("DISTRICT_EMBEDDING_QUERY_PREFIX", ""))
    district_embedding_passage_prefix: str = field(default_factory=lambda: os.getenv("DISTRICT_EMBEDDING_PASSAGE_PREFIX", ""))
    district_embedding_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("DISTRICT_EMBEDDING_TIMEOUT_SECONDS", "30")))
    # تتخطى الحالة الـLLM فقط إذا كان أقرب اسم دلالياً هو نتيجة القواعد نفسها
    # وبفارق لا يقل عن هذا الهامش عن الاسم التالي.
    district_embedding_agree_margin: float = field(default_factory=lambda: float(os.getenv("DISTRICT_EMBEDDING_AGREE_MARGIN", "0.03")))
    # نموذج Reranker المدرَّب في training/. فارغ = معطّل.
    district_reranker_path: str = field(default_factory=lambda: os.getenv("DISTRICT_RERANKER_PATH", ""))
    # decide: القواعد ثم النموذج ثم الـLLM حَكَماً فقط حين يختلف النموذج مع القواعد.
    # shadow: النموذج يعمل بالتوازي مع الـLLM ويُظهر اختياره في modelDistrict دون أن يقرّر.
    district_reranker_mode: str = field(default_factory=lambda: os.getenv("DISTRICT_RERANKER_MODE", "decide").strip().lower())
    district_reranker_log_path: Path = field(default_factory=lambda: Path(os.getenv(
        "DISTRICT_RERANKER_LOG_PATH", str(Path(__file__).resolve().parent / "features" / "district_correction" / "data" / "reranker_shadow.jsonl"))))

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

    def stt_model(self) -> SpeechToTextModel:
        """اختيار STT المركزي؛ ما تبقى شروط موديلات موزعة بالراوترات."""
        return SpeechToTextModel(self.whisper_model_ar, self.whisper_language_ar)

    def tts_model(self) -> TextToSpeechModel:
        """اختيار TTS المركزي، شاملاً كل ملفات نقطة التفتيش والمرجع."""
        return TextToSpeechModel(
            self.tts_model_ar,
            self.tts_ckpt_ar,
            self.tts_vocab_ar,
            self.tts_ref_audio_ar,
            self.tts_ref_text_ar,
        )


settings = Settings()
