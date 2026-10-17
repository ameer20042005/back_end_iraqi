# -*- coding: utf-8 -*-
"""طبقة اللهجة العراقية: ملحق مفردات البيع بالـ RAG والكتل المحقونة بالبرومت."""

from app.core.dialect import call_dialect_block, dialect_block, recent_user_text
from app.rag import search


def _words(query):
    return {r["word"] for r in search(query, top_k=8)}


def test_commerce_terms_are_retrievable():
    assert "حبة" in _words("اريد لابتوب لينوفو وحبة ماوس لوجيتك")
    assert "ثبتلي" in _words("ثبتلي واحد")
    assert "دزلي" in _words("دزلي حبتين")
    assert "عكب باجر" in _words("مو هسه عكب باجر")
    assert "يم" in _words("الكرادة يم ساحة الحرية")


def test_recent_user_text_reads_last_user_turns_and_content_parts():
    messages = [
        {"role": "user", "content": "هلا"},
        {"role": "assistant", "content": "هلا بيك"},
        {"role": "user", "content": [{"type": "text", "text": "شكد سعره"}, {"type": "image_url"}]},
    ]
    assert recent_user_text(messages) == "هلا\nشكد سعره"


def test_dialect_block_has_guide_and_rag_reference():
    block = dialect_block("دزلي حبتين")
    assert "فهم اللهجة العراقية" in block and "راح يوصلك" in block
    assert "- دزلي:" in block and "- حبتين:" in block


def test_call_block_has_scheduling_terms():
    block = call_dialect_block()
    assert "- عكب باجر:" in block and "- بطلت:" in block
