# -*- coding: utf-8 -*-
"""متانة حلقة الأدوات (app/tool_loop.py) أمام رد الموديل المقيَّد بـ guided_json.

الخلفية: البروتوكول عندنا مخطط JSON صارم (action: tool_call | final_answer)
مقيَّد بـ guided decoding (response_format.json_schema بجهة vLLM) — بدل
البروتوكول النصي القديم ([TOOL_CALL]{...}[/TOOL_CALL]) اللي كان عرضة لانحراف
الموديل عن الصيغة. guided_json يضمن JSON صالحاً نحوياً، لكن الحلقة تبقى
متحفّظة على شكل الحقول (data.get بدل data[]) لأن enum قد ينحرف بطرق أخرى.

التشغيل:  python -m pytest tests/test_tool_loop.py -v
"""

import asyncio
import json

import pytest

from app import tool_loop
from app.tool_loop import _EXHAUSTED_FALLBACK, build_schema, run_with_tools

_ORDER_STATUS_CALL = {
    "action": "tool_call",
    "tool_call": {"tool": "get_order_status", "args": {"order_id": "ORD-1001"}},
}


class _FakeEngine:
    """محرك وهمي يرجّع ردوداً محضّرة بالتسلسل — بلا أي اتصال بـ vLLM."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def render_prompt(self, messages, tools=None):
        return messages

    async def generate_full(self, prompt, result_holder=None, **kwargs):
        self.calls += 1
        reply = self._replies.pop(0) if self._replies else {"action": "final_answer", "final_answer": ""}
        text = json.dumps(reply, ensure_ascii=False) if not isinstance(reply, str) else reply
        if result_holder is not None:
            result_holder["stop_reason"] = None
            result_holder["finish_reason"] = "stop"
        return text


@pytest.fixture
def fake_engine(monkeypatch):
    def _install(replies):
        engine = _FakeEngine(replies)
        monkeypatch.setattr(tool_loop, "llm_engine", engine)
        return engine
    return _install


async def _ok_tool(args):
    return {"order_id": "ORD-1001", "status": "قيد التوصيل"}


def _run(replies, engine_factory, **kwargs):
    engine_factory(replies)
    return asyncio.run(
        run_with_tools([{"role": "user", "content": "وين طلبي"}],
                       tools={"get_order_status": _ok_tool}, **kwargs)
    )


def test_tool_result_reaches_final_answer(fake_engine):
    """المسار السليم: استدعاء ثم رد نهائي يعتمد على نتيجة الأداة."""
    data = _run(
        [_ORDER_STATUS_CALL, {"action": "final_answer", "final_answer": "طلبك قيد التوصيل"}],
        fake_engine,
    )
    assert data["final_answer"] == "طلبك قيد التوصيل"


def test_unknown_tool_reported_back_to_model(fake_engine):
    """أداة غير مسجَّلة: يرجع خطأ بنتيجة الأداة بدل ما ينهار، والموديل يكمل."""
    call = {"action": "tool_call", "tool_call": {"tool": "web_search", "args": {"query": "شي"}}}
    data = _run(
        [call, {"action": "final_answer", "final_answer": "ما اكدر اسويها"}],
        fake_engine,
    )
    assert data["final_answer"] == "ما اكدر اسويها"


def test_malformed_json_is_not_leaked(fake_engine):
    """رد ما يطابق JSON صالح إطلاقاً (نظرياً guided_json يمنعه، لكن الحلقة
    تبقى متحفّظة): يُحجب ويُستبدل برد آمن، بلا تسريب نص خام."""
    engine = fake_engine(["مو JSON صالح"])
    data = asyncio.run(
        run_with_tools([{"role": "user", "content": "وين طلبي"}],
                       tools={"get_order_status": _ok_tool})
    )
    assert data["final_answer"] == _EXHAUSTED_FALLBACK
    assert engine.calls == 1


def test_exhausted_rounds_return_fallback(fake_engine):
    """الموديل عالق يستدعي الأداة بكل جولة: العميل ياخذ رداً آمناً، مو آخر
    استدعاء أداة نصف مكتوب."""
    data = _run([_ORDER_STATUS_CALL] * 5, fake_engine, max_rounds=3)
    assert data["final_answer"] == _EXHAUSTED_FALLBACK


def test_plain_answer_passes_through(fake_engine):
    """رد عادي بلا أدوات يمر كما هو بجولة واحدة."""
    engine = fake_engine([{"action": "final_answer", "final_answer": "هلا بيك، شلون اساعدك؟"}])
    data = asyncio.run(
        run_with_tools([{"role": "user", "content": "هلا"}],
                       tools={"get_order_status": _ok_tool})
    )
    assert data["final_answer"] == "هلا بيك، شلون اساعدك؟"
    assert engine.calls == 1


def test_failing_tool_does_not_break_conversation(fake_engine):
    """أداة ترمي استثناء: الخطأ يُمرَّر للموديل ويكمل، بلا انهيار."""
    async def _boom(args):
        raise RuntimeError("الخدمة الخارجية واقعة")

    fake_engine([_ORDER_STATUS_CALL, {"action": "final_answer", "final_answer": "معذرة، صار خلل مؤقت"}])
    data = asyncio.run(
        run_with_tools([{"role": "user", "content": "وين طلبي"}],
                       tools={"get_order_status": _boom})
    )
    assert data["final_answer"] == "معذرة، صار خلل مؤقت"


def test_extra_schema_field_passed_through(fake_engine):
    """حقل إضافي بالمخطط (مثل order_ready بالمبيعات) يرجع بالـ dict كما هو
    بلا ما تفرضه الحلقة العامة أو تسقطه."""
    schema = build_schema(extra_properties={"order_ready": {"type": "boolean"}})
    engine = fake_engine([
        {"action": "final_answer", "final_answer": "زين، ثبّتلك الطلب", "order_ready": True}
    ])
    data = asyncio.run(
        run_with_tools([{"role": "user", "content": "اي اكيد"}],
                       tools={}, schema=schema)
    )
    assert data["final_answer"] == "زين، ثبّتلك الطلب"
    assert data["order_ready"] is True
    assert engine.calls == 1
