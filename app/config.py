# -*- coding: utf-8 -*-
"""إعدادات مشتركة (المحرك/RAG/الأدوات) — كلها قابلة للضبط عبر متغيرات بيئة.

تحذير أمني: لا تكتب أي قيمة سرّية (توكن/مفتاح) كافتراضي هنا مباشرة — هذا
الملف متتبَّع بـ git. كل الأسرار تُمرَّر فقط عبر متغيرات بيئة (`.env` محلياً
المستثنى بـ .gitignore، أو Environment Variables بإعدادات RunPod Pod).
"""

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # الموديل — النسخة المدموجة (base + LoRA اللهجة العراقية مندمجين بالأوزان
    # فعلياً عبر merge_and_unload، انظر gemma_iraqi_merge_fixed.ipynb) تشمل
    # أبراج الرؤية/الصوت كاملة. يُنزَّل تلقائياً من Hugging Face Hub عند أول
    # إقلاع (بدون أي أمر يدوي).
    model_name: str = "ameer4wisam/gemma-iraqi-finetune-v2"
    # مسار محوّل LoRA منفصل: غير مطلوب بعد الآن لأن model_name أعلاه مدموج
    # بالفعل. اتركه None إلا إذا رجعت لموديل base + محوّل غير مندمج.
    lora_path: Optional[str] = None
    lora_rank: int = 16  # تأكد من مطابقتها لقيمة "r" الفعلية بـ adapter_config.json على المستودع (فقط إن استُخدم lora_path)

    # توكن Hugging Face — مطلوب لأن Gemma موديل بوابة (gated) وربما مستودع
    # المحوّل خاص. لا قيمة افتراضية أبداً؛ يُقرأ فقط من متغير البيئة HF_TOKEN.
    hf_token: Optional[str] = None

    # دقّة الحساب — "auto"/"bfloat16" يحمّل bfloat16، أي قيمة أخرى تحمّل float16
    # (انظر app/engine.py: LLMEngine.start)
    dtype: str = "auto"
    # التكميم: غير مستخدَم حالياً بمحرك transformers (كان لـ vLLM فقط) — أُبقي
    # الحقل لعدم كسر .env قديمة، لكن app/engine.py لا يقرأه.
    quantization: Optional[str] = None

    # قفل طلب واحد بنفس اللحظة على GPU (asyncio.Lock بـ app/engine.py) — هذي
    # فقط حدود سعة سياق الموديل، مو batching حقيقي (انظر app/engine.py لتفاصيل
    # الاختيار عن vLLM/PagedAttention).
    gpu_memory_utilization: float = 0.85
    max_model_len: int = 4096
    max_num_seqs: int = 32  # أقصى عدد طلبات مجمّعة سوية (continuous batching)

    # Prefix Caching للبرومبت الثابت (system prompt + سياق RAG المتكرر)
    enable_prefix_caching: bool = True

    # توليد — الوصفة المعتمدة الوحيدة (خلية الاستدلال الاحترافية في
    # gemma_iraqi_merge_fixed.ipynb): **حتمي فقط** (temperature=0.0 →
    # do_sample=False في app/engine.py). أي sampling (حتى temperature=0.3
    # اللي كانت مضبوطة أيام vLLM) أنتج انهيار مخرجات كامل (هذيان غير مترابط)
    # مع هذا الموديل بالتجربة الفعلية على RunPod — "الوضع المتوازن محذوف
    # نهائياً" بنص النوتبوك. بدون repetition_penalty نهائياً لأنه كان يخرب
    # مفردات اللهجة العراقية. أجوبة التدريب قصيرة؛ الطول الزائد = هذيان.
    max_new_tokens: int = 64
    temperature: float = 0.0
    top_p: float = 1.0
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
