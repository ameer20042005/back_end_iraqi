# -*- coding: utf-8 -*-
"""أدوات مشتركة للتعامل مع Hugging Face Hub — مستخدَمة من app/engine.py (transformers)
وapp/features/order_intake/vision.py (transformers خام) كلاهما يحتاج نفس
منطق تحميل محوّل LoRA."""

import os
from typing import Optional

from huggingface_hub import snapshot_download


def resolve_lora_path(path: str, token: Optional[str] = None) -> str:
    """يرجع مساراً محلياً لمحوّل LoRA: كما هو إذا كان مساراً محلياً موجوداً،
    أو يحمّله تلقائياً من Hugging Face Hub إذا كان معرّف مستودع (namespace/name)."""
    if os.path.isdir(path):
        return path
    return snapshot_download(repo_id=path, token=token)
