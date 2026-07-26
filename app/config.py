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

    # طول الرد الأقصى — هذا **المقبض الوحيد الفعّال** لمرونة التوليد هنا.
    # تدرّج تاريخي: 64 (كانت تقصّ ردود المبيعات قسراً) → 150 → 256 الحالية.
    # 256 توكن ≈ 180-200 كلمة عربية، ويلزم منها الكثير عند **ملخّص الطلب**
    # (منتج + كمية + سعر + اسم + هاتف + عنوان بجملة وحدة، انظر
    # SALES_SYSTEM_PROMPT) — أطول رد بالمحادثة، وقصّه يقطع بيانات العميل.
    # زيادتها فوق 256 تزيد الإسهاب والكلفة بلا فائدة تُذكر لمحادثة مبيعات.
    max_new_tokens: int = 256

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
