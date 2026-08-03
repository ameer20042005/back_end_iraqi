# -*- coding: utf-8 -*-
"""إعدادات مشتركة (المحرك/RAG/الأدوات) — كلها قابلة للضبط عبر متغيرات بيئة.

تحذير أمني: لا تكتب أي قيمة سرّية (توكن/مفتاح) كافتراضي هنا مباشرة — هذا
الملف متتبَّع بـ git. كل الأسرار تُمرَّر فقط عبر متغيرات بيئة (`.env` محلياً
المستثنى بـ .gitignore، أو Environment Variables بإعدادات RunPod Pod).
"""

from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # الموديل — النسخة المدموجة (base + LoRA اللهجة العراقية مندمجين بالأوزان
    # فعلياً عبر merge_and_unload، انظر gemma_iraqi_merge_fixed.ipynb) تشمل
    # أبراج الرؤية/الصوت كاملة. يخدمه خادم vLLM منفصل (انظر start.sh) —
    # القيمة هنا تُستخدم باسم الموديل بطلبات /v1/chat/completions ويقرأها
    # start.sh لتمريرها لـ `vllm serve`.
    model_name: str = "ameer4wisam/gemma-iraqi-10k-merged"

    # عناوين خوادم vLLM OpenAI-متوافقة — الباك اند عميل HTTP رفيع فقط
    # (انظر app/engine.py). محلياً بدون أي خادم يبقى ready=False وكل الميزات
    # ترجع لوضع fallback.
    #
    # نسختان (أو أكثر) من نفس الموديل بالتوازي على نفس كارت GPU (start.sh
    # يشغّلهن فعلياً حسب VLLM_REPLICAS، افتراضياً 2 — كل نسخة على منفذ
    # VLLM_PORT+i بحصة VRAM مقسّمة). engine.py يوزّع الطلبات بينهن round-robin
    # مع تتبّع جاهزية مستقلة لكل نسخة، فطلب طويل بنسخة وحدة ما يعطّل الثانية.
    #
    # مفصولة بفواصل بمتغير بيئة واحد VLLM_BASE_URLS (مثلاً
    # "http://127.0.0.1:18001/v1,http://127.0.0.1:18002/v1"). القيمة
    # الافتراضية هنا تُبنى من vllm_base_url (المنفذ الأساسي) + عدد النسخ
    # الافتراضي بـ start.sh، فلا حاجة لضبط شيء إذا التزمت بالإعداد الافتراضي.
    # ⚠️ لازم تطابق منافذ start.sh حرفياً (VLLM_PORT وVLLM_REPLICAS) — لا ربط
    # تلقائي بينهما، فأي تغيير بأحدهما يتطلب تحديث الآخر يدوياً.
    vllm_base_url: str = "http://127.0.0.1:18001/v1"
    vllm_base_urls: Optional[str] = None
    # عدد النسخ الافتراضي عند عدم ضبط VLLM_BASE_URLS يدوياً — لازم يطابق
    # VLLM_REPLICAS بـ start.sh (يُستخدم فقط لبناء القائمة تلقائياً من
    # vllm_base_url بزيادة رقم المنفذ لكل نسخة؛ لا يوقف vLLM نفسه).
    vllm_replicas: int = 2

    @property
    def resolved_vllm_base_urls(self) -> list[str]:
        """قائمة عناوين خوادم vLLM الفعلية — من VLLM_BASE_URLS (مفصولة
        بفواصل) إن وُجدت، وإلا تُبنى تلقائياً من vllm_base_url بزيادة رقم
        المنفذ لكل نسخة حسب vllm_replicas (يطابق تسلسل منافذ start.sh)."""
        if self.vllm_base_urls:
            return [u.strip() for u in self.vllm_base_urls.split(",") if u.strip()]

        parsed = urlsplit(self.vllm_base_url)
        if parsed.port is None:
            return [self.vllm_base_url]
        urls = []
        for i in range(self.vllm_replicas):
            netloc = f"{parsed.hostname}:{parsed.port + i}"
            urls.append(urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)))
        return urls

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
    whisper_model: str = "ayoubkirouane/whisper-small-ar"

    # تحويل نص لصوت باللهجة العراقية (app/features/voice_followup/tts.py) —
    # يخدم مسار المتابعة الصوتية للطلبات (سؤال الزبون سبب الرفض/الإلغاء
    # صوتياً). موديل transformers منفصل عن Whisper، يعيش بعملية FastAPI نفسها.
    tts_model: str = "ameer4wisam/Habibi-TTS-IRQ"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        # حقول قديمة بملفات .env سابقة (lora_path, dtype, quantization...)
        # ما عادت مستخدمة بعد الانتقال لخادم vLLM — نتجاهلها بدل كسر الإقلاع.
        extra = "ignore"


settings = Settings()
