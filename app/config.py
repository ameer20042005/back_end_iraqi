# -*- coding: utf-8 -*-
"""إعدادات المحرك (vLLM) والـ RAG — كلها قابلة للضبط عبر متغيرات بيئة."""

from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # الموديل
    model_name: str = "Qwen/Qwen2.5-1.5B-Instruct"
    lora_path: Optional[str] = None  # مسار محوّل LoRA، مثال: fine_tuning/output
    lora_rank: int = 16

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

    # توليد
    max_new_tokens: int = 512
    temperature: float = 0.7
    system_prompt: str = (
        "أنت مساعد ذكي يتحدث ويفهم اللهجة العراقية. "
        "أجب بإيجاز ووضوح، واستخدم المعلومات المرجعية إن كانت مفيدة."
    )

    # RAG
    rag_top_k: int = 5

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
