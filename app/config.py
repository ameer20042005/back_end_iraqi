# -*- coding: utf-8 -*-
"""إعدادات مشتركة (المحرك/RAG/الأدوات) — كلها قابلة للضبط عبر متغيرات بيئة.

تحذير أمني: لا تكتب أي قيمة سرّية (توكن/مفتاح) كافتراضي هنا مباشرة — هذا
الملف متتبَّع بـ git. كل الأسرار تُمرَّر فقط عبر متغيرات بيئة (`.env` محلياً
المستثنى بـ .gitignore، أو Environment Variables بإعدادات RunPod Pod).
"""

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # الموديل — يُنزَّل تلقائياً من Hugging Face Hub عند أول إقلاع (بدون أي أمر يدوي)
    model_name: str = "google/gemma-4-E4B-it"
    # مسار محوّل LoRA: مسار محلي، أو معرّف مستودع HF (مثال أدناه) فيُنزَّل تلقائياً أيضاً
    lora_path: Optional[str] = "ameer4wisam/gemma-iraqi-finetune"
    lora_rank: int = 16  # تأكد من مطابقتها لقيمة "r" الفعلية بـ adapter_config.json على المستودع

    # توكن Hugging Face — مطلوب لأن Gemma موديل بوابة (gated) وربما مستودع
    # المحوّل خاص. لا قيمة افتراضية أبداً؛ يُقرأ فقط من متغير البيئة HF_TOKEN.
    hf_token: Optional[str] = None

    # دقّة الحساب — "auto" يترك vLLM يقرر حسب العتاد، أو "float16"/"bfloat16"
    dtype: str = "auto"
    # التكميم: None لـ FP16/BF16 الكامل، أو "bitsandbytes" لـ INT8 (إن كانت نسخة vLLM تدعمها)
    quantization: Optional[str] = None

    # PagedAttention + Continuous Batching (مدمجة في vLLM تلقائياً، هذي فقط حدود السعة)
    gpu_memory_utilization: float = 0.85
    max_model_len: int = 4096
    max_num_seqs: int = 32  # أقصى عدد طلبات مجمّعة سوية (continuous batching)

    # Prefix Caching للبرومبت الثابت (system prompt + سياق RAG المتكرر)
    enable_prefix_caching: bool = True

    # توليد — القيم مستخلصة من تجارب llm_iraqi_best.ipynb (بدون repetition_penalty
    # لأنه كان يخرب مفردات اللهجة العراقية بالتجربة الفعلية)
    max_new_tokens: int = 512
    temperature: float = 0.7
    top_p: float = 0.8
    top_k: int = 20

    # RAG (لهجة عراقية + منتجات)
    rag_top_k: int = 5

    # ملاحظة: أداة البحث بالإنترنت (app/tools/web_search.py) تستخدم DuckDuckGo
    # عبر مكتبة ddgs — بدون أي مفتاح/إعداد مطلوب هنا.

    # تحويل الصوت لنص (app/features/order_intake/transcribe.py) — موديل Whisper
    # مفرَّغ عليه اللهجة العربية (نموذج transformers عادي، وليس CTranslate2)
    whisper_model: str = "ayoubkirouane/whisper-small-ar"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
