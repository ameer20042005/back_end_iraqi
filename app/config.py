# -*- coding: utf-8 -*-
"""إعدادات التطبيق الثابتة.

هذا هو مصدر إعدادات التطبيق و``start.sh``. تُقرأ الأسرار ومتغيرات ميزة
تصحيح المناطق من البيئة؛ بقية القيم تُضبط هنا مباشرة.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _env_flag(name: str) -> Optional[bool]:
    """1/true/yes أو 0/false/no؛ غير مضبوط (أو فارغ) = None (القرار للافتراضي)."""
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return None
    return raw not in ("0", "false", "no", "off")


def _gpu_memory_utilization() -> float:
    """GPU_MEMORY_UTILIZATION من البيئة (بين 0.1 و0.95) وإلا 0.70."""
    value = float(os.getenv("GPU_MEMORY_UTILIZATION") or "0.70")
    if not 0.1 <= value <= 0.95:
        raise ValueError(f"GPU_MEMORY_UTILIZATION لازم بين 0.1 و0.95، والقيمة {value}")
    return value


@dataclass(frozen=True)
class TextToSpeechModel:
    """موديل النطق العربي ونسخته ولغته ومقطعه المرجعي، من مصدر إعداد واحد."""

    repository: str
    checkpoint: str
    language: str
    reference_audio: str


@dataclass(frozen=True)
class Settings:
    # Qwen3.5-9B للنص والصور؛ Whisper مستقل لفهم الصوت.
    # يخدم Qwen خادم vLLM منفصل (انظر start.sh) —
    # القيمة هنا تُستخدم باسم الموديل بطلبات /v1/chat/completions ويقرأها
    # start.sh لتمريرها لـ `vllm serve`.
    # start.sh يقرأ هذه القيمة مباشرة، لذلك لا يوجد مصدر ثانٍ قد يختلف عنها.
    #
    # التشغيل المحلي (start_local.sh) يتجاوز الاسم والرابط بمتغيرَي البيئة
    # LLM_MODEL وLLM_BASE_URL (افتراضياً http://localhost:1234/v1). start.sh ما
    # يضبطهما أبداً، فتبقى القيم الثابتة على RunPod. `or` بدل قيمة getenv
    # الافتراضية حتى ما تصير القيمة فارغة لو صُدِّر المتغير فارغاً.
    model_name: str = field(default_factory=lambda: os.getenv("LLM_MODEL") or "Qwen/Qwen3.5-9B")

    # عنوان خادم vLLM OpenAI-متوافق — الباك اند عميل HTTP رفيع فقط (انظر
    # app/core/engine.py). محلياً بدون أي خادم يبقى ready=False وكل الميزات ترجع
    # لوضع fallback.
    # يطابق vllm_port أدناه؛ غيّرهما معاً عند نقل الخادم إلى منفذ آخر.
    vllm_base_url: str = field(default_factory=lambda: os.getenv("LLM_BASE_URL") or "http://127.0.0.1:18001/v1")

    # مهلة طلب توليد واحد بالثواني. الموديلات المحلية مع التفكير قد تأخذ
    # دقيقة؛ تجاوزها يرجع AI_REQUEST_TIMEOUT (504) بدل تعليق الطلب.
    llm_request_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS") or "120"))

    # Qwen وWhisper وChatterbox مستودعات عامة؛ لا تحتاج توكن للتنزيل.

    # vLLM weights, activations and KV are separate allocations. By default
    # reserve 4 GiB for KV, rather than allocating a fraction of the whole GPU.
    # GPU_MEMORY_UTILIZATION applies only when KV_CACHE_MEMORY_GB=0.
    # The rest is left for Whisper, Chatterbox and CUDA runtime/encoder overhead.
    gpu_memory_utilization: float = field(default_factory=_gpu_memory_utilization)
    max_model_len: int = 32768
    max_num_seqs: int = field(default_factory=lambda: max(1, int(os.getenv("VLLM_MAX_NUM_SEQS") or "16")))
    kv_cache_memory_gb: float = field(default_factory=lambda: max(0.0, float(
        os.getenv("KV_CACHE_MEMORY_GB") or "4")))
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
    # الحقيقية (انظر app/orders/gateway.py). هذا الباك اند
    # (الذكاء) ما يخزّن ولا منتج ولا طلب محلياً — استعلامات المنتجات
    # وإرسال الطلبات تمر لحظياً عبر HTTP لهذا العنوان
    # ولا يُحفظ. رابط ومسارات باك اند السستم الفعلية غير معروفة بعد؛ عدّل
    # القيمة هنا عند توفرها.
    system_backend_base_url: str = "http://127.0.0.1:8081/internal"
    # مسار إرسال نتيجة المكالمة لباك اند السستم (voice_followup/gateway.py).
    # فارغ = ما ترسل؛ النتيجة تبقى بالمخزن المحلي دائماً.
    system_backend_call_outcome_path: str = field(
        default_factory=lambda: os.getenv("SYSTEM_BACKEND_CALL_OUTCOME_PATH", "/calls/outcome"))

    # باك اند البيانات "Catalog" (متاجر جيني) — app/core/catalog.py. منه
    # أدوات البيانات بالمكالمة (طلبات الزبون، المنتجات) وبمساعد الموظف. كل القيم
    # من البيئة؛ CATALOG_BASE_URL فارغ = الأدوات ما تتسجل أصلاً.
    catalog_base_url: str = field(default_factory=lambda: os.getenv("CATALOG_BASE_URL", "").rstrip("/"))
    # JWT (Bearer) — سر، من البيئة فقط.
    catalog_access_token: str = field(default_factory=lambda: os.getenv("CATALOG_ACCESS_TOKEN", ""))
    # يُرسل بهيدر X-Tenant-Id.
    catalog_tenant_id: str = field(default_factory=lambda: os.getenv("CATALOG_TENANT_ID", ""))
    catalog_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("CATALOG_TIMEOUT_SECONDS") or "15"))
    # مخزن المتجر: لو مضبوط، التوفّر والسعر من stock infos هذا المخزن بس (مثل ai_agent).
    catalog_storage_id: str = field(default_factory=lambda: os.getenv("CATALOG_STORAGE_ID", ""))
    # أحجام صفحات صغيرة حتى يبقى زمن الرد بالمكالمة قليل.
    catalog_search_page_size: int = field(default_factory=lambda: int(os.getenv("CATALOG_SEARCH_PAGE_SIZE") or "5"))
    catalog_orders_page_size: int = field(default_factory=lambda: int(os.getenv("CATALOG_ORDERS_PAGE_SIZE") or "10"))
    catalog_stock_page_size: int = field(default_factory=lambda: int(os.getenv("CATALOG_STOCK_PAGE_SIZE") or "20"))
    catalog_path_product_search: str = field(default_factory=lambda: os.getenv(
        "CATALOG_PATH_PRODUCT_SEARCH", "/api/catalog/v1/products/search"))
    catalog_path_product: str = field(default_factory=lambda: os.getenv(
        "CATALOG_PATH_PRODUCT", "/api/catalog/v1/products/{id}"))
    catalog_path_categories: str = field(default_factory=lambda: os.getenv(
        "CATALOG_PATH_CATEGORIES", "/api/catalog/v1/categories/all"))
    catalog_path_product_stock: str = field(default_factory=lambda: os.getenv(
        "CATALOG_PATH_PRODUCT_STOCK", "/api/catalog/v1/stock-infos/product/{productId}"))
    catalog_path_customer_orders: str = field(default_factory=lambda: os.getenv(
        "CATALOG_PATH_CUSTOMER_ORDERS", "/api/catalog/v1/online/sells/all"))
    catalog_path_order: str = field(default_factory=lambda: os.getenv(
        "CATALOG_PATH_ORDER", "/api/catalog/v1/online/sells/{id}"))
    # بحث الطلبات لمساعد الموظف: نفس قائمة /online/sells/all اللي تقبل
    # customerPhoneNumber وcreatedAt (يوم واحد) وflowStepId وreceiptNumber.
    catalog_path_orders_search: str = field(default_factory=lambda: os.getenv(
        "CATALOG_PATH_ORDERS_SEARCH", "/api/catalog/v1/online/sells/all"))
    # فارغ = ما ترسل نتيجة المكالمة لباك اند البيانات.
    catalog_path_call_outcome: str = field(default_factory=lambda: os.getenv("CATALOG_PATH_CALL_OUTCOME", ""))

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
        "DISTRICT_SOURCE_DIR", str(Path(__file__).resolve().parent.parent / "data" / "address"))))
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

    # تحويل نص لصوت (app/features/voice_followup/tts.py) — صوت الـ agent
    # بالمكالمات الحية. موديل Chatterbox Multilingual (Resemble AI) يدعم
    # العربية (ar) مع voice cloning من مقطع مرجعي، يعيش بعملية الـ agent worker.
    tts_model_ar: str = "ResembleAI/chatterbox"
    # نسخة نقطة تفتيش T3 متعددة اللغات: "v2" (الافتراضي بالمكتبة) أو "v3".
    tts_ckpt_ar: str = "v3"
    tts_language_ar: str = "ar"
    # Chatterbox ما يحتاج نص المرجع، ويستعمل أول ~10 ثوانٍ من المقطع فقط.
    tts_ref_audio_ar: str = "reference/mazen.mp3"
    # عدد نسخ Chatterbox بالـ worker: كل نسخة تنطق لمكالمة وحدة بالمرة (المكتبة
    # مو آمنة للاستدعاء المتوازي على نفس النسخة)، فالنسخ الأكثر = مكالمات
    # متزامنة تنتظر أقل، مقابل ~3GB VRAM تقريباً لكل نسخة. تُحمَّل عند الحاجة.
    tts_pool_size: int = field(default_factory=lambda: max(1, int(os.getenv("TTS_POOL_SIZE") or "1")))
    # الوكيل المحلي عبر WebSocket (app/features/voice_followup/stream.py):
    # تطبيق سطح المكتب يتصل بالزبون من رقم الواتساب مالته ويبث صوت المكالمة
    # لـ WS /voice_followup/stream، والحوار عبر Whisper ثم Qwen ثم Chatterbox.
    # يعيش بعملية uvicorn نفسها عبر Pipecat.
    # أقصى مكالمات WebSocket متزامنة؛ بعدها الاتصال يُرفض بـ busy (4429).
    voice_stream_max_calls: int = field(default_factory=lambda: max(1, int(
        os.getenv("VOICE_STREAM_MAX_CALLS") or os.getenv("VOICE_MAX_CALLS") or "10")))
    # Shared Whisper instance for live calls and uploaded voice notes.
    stt_model: str = "ayoubkirouane/whisper-small-ar"
    stt_device: str = field(default_factory=lambda: os.getenv("STT_DEVICE") or "auto")
    stt_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("STT_TIMEOUT_SECONDS") or "30"))
    call_outcome_submit_timeout_seconds: float = 2.0

    voice_stream_first_phrase_chars: int = field(default_factory=lambda: max(16, min(200, int(
        os.getenv("VOICE_STREAM_FIRST_PHRASE_CHARS") or "56"))))
    # كاشف الكلام: "silero" (ONNX، أدق)، "energy" (طاقة الصوت، بلا تبعيات)،
    # أو "auto" = silero لو onnxruntime وأوزانه موجودة، وإلا energy.
    voice_stream_vad: str = field(default_factory=lambda: (os.getenv("VOICE_STREAM_VAD") or "auto").strip().lower())
    voice_stream_silero_threshold: float = field(
        default_factory=lambda: float(os.getenv("VOICE_STREAM_SILERO_THRESHOLD") or "0.5"))
    voice_stream_energy_threshold_dbfs: float = field(
        default_factory=lambda: float(os.getenv("VOICE_STREAM_ENERGY_DBFS") or "-40"))
    # أقل كلام متواصل يُعتبر بداية دور (ويقاطع الـ agent)، والسكوت اللي ينهي الدور.
    voice_stream_min_speech_ms: int = field(default_factory=lambda: int(os.getenv("VOICE_STREAM_MIN_SPEECH_MS") or "250"))
    voice_stream_end_silence_ms: int = field(default_factory=lambda: int(os.getenv("VOICE_STREAM_END_SILENCE_MS") or "400"))
    # مقطع أطول من هذا ينقطع ويُنسخ حتى لو الزبون بعده يحچي.
    voice_stream_max_segment_seconds: float = field(
        default_factory=lambda: float(os.getenv("VOICE_STREAM_MAX_SEGMENT_SECONDS") or "15"))
    # بلا كلام من الزبون ولا من الـ agent هذه المدة بعد الرد = المكالمة تنتهي.
    voice_stream_idle_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("VOICE_STREAM_IDLE_TIMEOUT_SECONDS") or "60"))
    voice_stream_max_call_seconds: float = field(
        default_factory=lambda: float(os.getenv("VOICE_STREAM_MAX_CALL_SECONDS") or "900"))
    # كم يسبق إرسال صوت الـ agent وقت تشغيله عند التطبيق: أقل = مقاطعة أنظف.
    voice_stream_tts_lead_ms: int = field(default_factory=lambda: int(os.getenv("VOICE_STREAM_TTS_LEAD_MS") or "1500"))
    # Allow complete save_outcome JSON for multi-question calls. Spoken replies
    # stay short through the prompt; this cap does not delay streamed phrases.
    voice_stream_llm_max_tokens: int = field(default_factory=lambda: int(os.getenv("VOICE_STREAM_LLM_MAX_TOKENS") or "1024"))
    # تطبيع مستوى صوت الزبون (AGC) قبل VAD والنموذج: يرفع الصوت الواطي لحد
    # الهدف بسقف رفع، بلا خفض. 0 يعطّله.
    voice_stream_agc: bool = field(default_factory=lambda: _env_flag("VOICE_STREAM_AGC") is not False)
    voice_stream_agc_target_dbfs: float = field(
        default_factory=lambda: float(os.getenv("VOICE_STREAM_AGC_TARGET_DBFS") or "-20"))
    voice_stream_agc_max_gain_db: float = field(
        default_factory=lambda: float(os.getenv("VOICE_STREAM_AGC_MAX_GAIN_DB") or "20"))
    # تحميل Chatterbox مسبقاً عند إقلاع uvicorn (بعد جاهزية vLLM حتى
    # يحجز ذاكرته أول): 1 = دائماً، 0 = أبداً (تُحمَّل بأول مكالمة)، غير مضبوط =
    # على GPU بس. يطلّع أي فشل تحميل عند البدء بدل أول مكالمة.
    voice_stream_warmup: Optional[bool] = field(default_factory=lambda: _env_flag("VOICE_STREAM_WARMUP"))

    # نتائج المكالمات (app/features/voice_followup/outcomes.py): ملف JSON لكل غرفة.
    # الـ agent worker عملية منفصلة عن uvicorn، فالملفات هي المخزن المشترك بينهما،
    # وتطبيق سطح المكتب يسحبها بـ GET /voice_followup/calls/{room}/outcome.
    call_outcomes_dir: Path = field(default_factory=lambda: Path(os.getenv(
        "CALL_OUTCOMES_DIR", str(Path(__file__).resolve().parent.parent / "data" / "call_outcomes"))))
    call_outcomes_ttl_hours: float = field(default_factory=lambda: float(os.getenv("CALL_OUTCOMES_TTL_HOURS", "24")))
    call_outcomes_max_files: int = field(default_factory=lambda: max(1, int(os.getenv("CALL_OUTCOMES_MAX_FILES", "5000"))))

    def tts_model(self) -> TextToSpeechModel:
        """اختيار TTS المركزي، شاملاً كل ملفات نقطة التفتيش والمرجع."""
        return TextToSpeechModel(
            self.tts_model_ar,
            self.tts_ckpt_ar,
            self.tts_language_ar,
            self.tts_ref_audio_ar,
        )


settings = Settings()
