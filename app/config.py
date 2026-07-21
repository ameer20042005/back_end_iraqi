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
    # أبراج الرؤية/الصوت كاملة. يخدمه خادم vLLM منفصل (انظر start.sh) —
    # القيمة هنا تُستخدم باسم الموديل بطلبات /v1/chat/completions ويقرأها
    # start.sh لتمريرها لـ `vllm serve`.
    model_name: str = "ameer4wisam/gemma-iraqi-finetune-v2"

    # عنوان خادم vLLM OpenAI-متوافق — الباك اند عميل HTTP رفيع فقط
    # (انظر app/engine.py). محلياً بدون الخادم يبقى ready=False وكل الميزات
    # ترجع لوضع fallback.
    # ⚠️ لازم يطابق VLLM_PORT بـ start.sh حرفياً (18001 افتراضياً، مو 8001 —
    # بعض قوالب RunPod العامة عندها nginx داخلي ماسك 8001 مسبقاً، لاحظناه
    # فعلياً بالنشر). لو غيّرت VLLM_PORT بمتغير بيئة، غيّر هذا معه بنفس القيمة.
    vllm_base_url: str = "http://127.0.0.1:18001/v1"

    # توكن Hugging Face — مطلوب لأن Gemma موديل بوابة (gated) وربما مستودع
    # الموديل خاص. لا قيمة افتراضية أبداً؛ يُقرأ فقط من متغير البيئة HF_TOKEN
    # (يقرأه start.sh ويمرره لخادم vLLM).
    hf_token: Optional[str] = None

    # إعدادات خادم vLLM (يقرأها start.sh ويمررها كأعلام لـ `vllm serve`):
    # نسبة VRAM المحجوزة للموديل + KV cache — 0.90 حسب الوصفة الرسمية.
    gpu_memory_utilization: float = 0.90
    # طول السياق الأقصى — أقصر = مساحة KV cache أكبر = طلبات متزامنة أكثر.
    # 4096 يكفي لمحادثة مبيعات + كتالوج RAG بسهولة.
    max_model_len: int = 4096

    # توليد — الوصفة المعتمدة الوحيدة (خلية الاستدلال الاحترافية في
    # gemma_iraqi_merge_fixed.ipynb): **حتمي فقط** (temperature=0.0 دائماً في
    # app/engine.py). أي sampling (حتى temperature=0.3) أنتج انهيار مخرجات
    # كامل (هذيان غير مترابط) مع هذا الموديل بالتجربة الفعلية على RunPod.
    # رُفع من 64 إلى 150 لأن 64 كانت تقصّ ردود المبيعات الطبيعية قسراً
    # (بائع حقيقي يحتاج جملتين-ثلاث أحياناً، مو جملة واحدة دائماً) — لو
    # لاحظت هذياناً بأطوال أعلى مستقبلاً رجّعها لـ64.
    max_new_tokens: int = 150
    temperature: float = 0.0

    # RAG (لهجة عراقية + منتجات)
    rag_top_k: int = 5

    # ملاحظة: أداة البحث بالإنترنت (app/tools/web_search.py) تستخدم DuckDuckGo
    # عبر مكتبة ddgs — بدون أي مفتاح/إعداد مطلوب هنا.

    # تحويل الصوت لنص (app/features/order_intake/transcribe.py) — موديل Whisper
    # مفرَّغ عليه اللهجة العربية (نموذج transformers عادي، يعمل بعملية FastAPI
    # نفسها — الصوت لا يمر بخادم vLLM)
    whisper_model: str = "ayoubkirouane/whisper-small-ar"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # حقول قديمة بملفات .env سابقة (lora_path, dtype, quantization...)
        # ما عادت مستخدمة بعد الانتقال لخادم vLLM — نتجاهلها بدل كسر الإقلاع.
        extra = "ignore"


settings = Settings()
