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
from app.tool_loop import (
    EXHAUSTED_FALLBACK,
    answer_without_tools,
    build_schema,
    run_decision_rounds,
    run_with_tools,
    stream_final_answer,
)

_ORDER_STATUS_CALL = {
    "action": "tool_call",
    "tool_call": {"tool": "get_order_status", "args": {"order_id": "ORD-1001"}},
}


class _FakeEngine:
    """محرك وهمي يرجّع ردوداً محضّرة بالتسلسل — بلا أي اتصال بـ vLLM."""

    def __init__(self, replies, stream_chunks=None):
        self._replies = list(replies)
        self._stream_chunks = list(stream_chunks or [])
        self.calls = 0
        self.stream_calls = 0
        self.last_multi_modal_data = None

    def render_prompt(self, messages, tools=None):
        return messages

    async def generate_full(self, prompt, result_holder=None, **kwargs):
        self.calls += 1
        self.last_multi_modal_data = kwargs.get("multi_modal_data")
        reply = self._replies.pop(0) if self._replies else {"action": "final_answer", "final_answer": ""}
        text = json.dumps(reply, ensure_ascii=False) if not isinstance(reply, str) else reply
        if result_holder is not None:
            result_holder["stop_reason"] = None
            result_holder["finish_reason"] = "stop"
        return text

    async def stream_chat_completion(self, prompt, max_tokens=None, stop=None, multi_modal_data=None):
        self.stream_calls += 1
        self.last_multi_modal_data = multi_modal_data
        for chunk in self._stream_chunks:
            yield chunk


@pytest.fixture
def fake_engine(monkeypatch):
    def _install(replies, stream_chunks=None):
        engine = _FakeEngine(replies, stream_chunks=stream_chunks)
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
    assert data["final_answer"] == EXHAUSTED_FALLBACK
    assert engine.calls == 1


def test_exhausted_rounds_return_fallback(fake_engine):
    """الموديل عالق يستدعي الأداة بكل جولة: العميل ياخذ رداً آمناً، مو آخر
    استدعاء أداة نصف مكتوب."""
    data = _run([_ORDER_STATUS_CALL] * 5, fake_engine, max_rounds=3)
    assert data["final_answer"] == EXHAUSTED_FALLBACK


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


# ---------------------------------------------------------------------------
# run_decision_rounds / stream_final_answer — البروتوكول المُقسَّم للبث
# (انظر app/tool_loop.py: فصل قرار الأداة عن النص النهائي الحر).
# ---------------------------------------------------------------------------

def test_decision_rounds_calls_tool_then_stops(fake_engine):
    """جولة أداة ثم action=done: تتوقف بلا توليد نص، وترجع working_messages
    جاهزة لجولة البث + بيانات القرار (order_ready هنا)."""
    engine = fake_engine([
        _ORDER_STATUS_CALL,
        {"action": "done", "order_ready": True},
    ])
    working_messages, decision, tool_calls = asyncio.run(
        run_decision_rounds(
            [{"role": "user", "content": "وين طلبي"}],
            tools={"get_order_status": _ok_tool},
        )
    )
    assert decision == {"action": "done", "order_ready": True}
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool"] == "get_order_status"
    assert engine.calls == 2
    # working_messages تراكمت رسالتي الأداة (assistant + user) فوق الأصل.
    assert len(working_messages) == 3


def test_decision_rounds_exhausted_returns_empty_decision(fake_engine):
    """الموديل عالق يستدعي الأداة بكل جولة: بعد max_rounds ترجع decision
    فارغة (بلا انهيار) — الراوتر يعامل هذا كعدم اكتمال (order_ready=False)."""
    fake_engine([_ORDER_STATUS_CALL] * 5)
    _working_messages, decision, _tool_calls = asyncio.run(
        run_decision_rounds(
            [{"role": "user", "content": "وين طلبي"}],
            tools={"get_order_status": _ok_tool},
            max_rounds=3,
        )
    )
    assert decision == {}


def test_stream_final_answer_yields_deltas_in_order(fake_engine):
    """stream_final_answer يمرر دلتات stream_chat_completion كما هي، بالترتيب،
    بلا guided_json (توليد حر)."""
    engine = fake_engine([], stream_chunks=["هلا ", "بيك ", "حبيبي"])
    deltas = asyncio.run(_collect_stream(
        stream_final_answer([{"role": "user", "content": "هلا"}])
    ))
    assert deltas == ["هلا ", "بيك ", "حبيبي"]
    assert engine.stream_calls == 1


async def _collect_stream(agen):
    return [chunk async for chunk in agen]


# ---------------------------------------------------------------------------
# answer_without_tools — مسار app/intent_router.py::is_pure_chitchat (رد
# مباشر بلا جولات أدوات ولا guided_json، انظر next.md §2).
# ---------------------------------------------------------------------------

def test_answer_without_tools_collects_stream_deltas(fake_engine):
    """يجمع دلتات stream_final_answer لرد واحد — بلا guided_json ولا أداة."""
    engine = fake_engine([], stream_chunks=["هلا ", "بيك ", "شلونك"])
    answer = asyncio.run(
        answer_without_tools([{"role": "user", "content": "هلا"}])
    )
    assert answer == "هلا بيك شلونك"
    assert engine.stream_calls == 1
    assert engine.calls == 0  # ماكو أي جولة guided_json — توليد حر فقط


def test_answer_without_tools_falls_back_on_empty_stream(fake_engine):
    """بث فاضي (عطل مؤقت بجهة vLLM) يرجع EXHAUSTED_FALLBACK بدل نص فاضي."""
    fake_engine([], stream_chunks=[])
    answer = asyncio.run(
        answer_without_tools([{"role": "user", "content": "هلا"}])
    )
    assert answer == EXHAUSTED_FALLBACK


def test_answer_without_tools_forwards_multi_modal_data(fake_engine):
    """صورة مرفقة (app/features/sales/router.py) لازم توصل استدعاء المحرك
    الفعلي، لا تضيع بين answer_without_tools وstream_final_answer."""
    engine = fake_engine([], stream_chunks=["زين"])
    image_marker = {"image": object()}
    asyncio.run(
        answer_without_tools(
            [{"role": "user", "content": "شنو هذا؟"}], multi_modal_data=image_marker,
        )
    )
    assert engine.last_multi_modal_data is image_marker


def test_run_with_tools_forwards_multi_modal_data(fake_engine):
    """نفس الشي لجولة guided_json (run_with_tools) — الصورة تبقى مرفقة
    بكل جولة توليد حتى لو استدعى الموديل أداة أولاً."""
    engine = fake_engine([{"action": "final_answer", "final_answer": "زين"}])
    image_marker = {"image": object()}
    asyncio.run(
        run_with_tools(
            [{"role": "user", "content": "شنو هذا؟"}], tools={}, multi_modal_data=image_marker,
        )
    )
    assert engine.last_multi_modal_data is image_marker
