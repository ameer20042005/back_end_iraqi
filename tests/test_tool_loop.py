# -*- coding: utf-8 -*-
"""متانة حلقة الأدوات (app/tool_loop.py) أمام انحرافات الموديل عن الصيغة.

الخلفية: البروتوكول عندنا نصّي (`[TOOL_CALL]{...}[/TOOL_CALL]`) مو
function-calling أصلي، والموديل مرصود إنه ما يلتزم بالصيغة حرفياً (انظر
TRAINING_DATA_PROMPT.md — النمط أصلاً غير موجود ببيانات التدريب). كل انحراف
كان يُسقِط الاستدعاء بصمت، فيصير أحد أمرين، وكلاهما مرفوض:

  1. النص الخام («[TOOL_CALL]{...}») يوصل العميل كما هو.
  2. الموديل «ما يشوف» نتيجة الأداة فيخترع الجواب — نفس هلوسة ORD-1002.

التشغيل:  python -m pytest tests/test_tool_loop.py -v
"""

import asyncio

import pytest

from app import tool_loop
from app.tool_loop import _EXHAUSTED_FALLBACK, _extract_tool_call, run_with_tools

# --------------------------------------------------------------------------
# استخراج الاستدعاء من صيغه المنحرفة
# --------------------------------------------------------------------------

_CALL = '{"tool": "get_order_status", "args": {"order_id": "ORD-1001"}}'

EXTRACT_CASES = [
    ("الصيغة القياسية", f"[TOOL_CALL]{_CALL}", True),
    ("كلام قبل الاستدعاء", f"خليني اشوفلك [TOOL_CALL]{_CALL}", True),
    ("كلام بعد الاستدعاء", f"[TOOL_CALL]{_CALL} راح اشوفلك", True),
    ("وسم ختامي كامل", f"[TOOL_CALL]{_CALL}[/TOOL_CALL]", True),
    ("وسم ختامي وكلام بعده", f"[TOOL_CALL]{_CALL}[/TOOL_CALL] لحظة", True),
    ("JSON عارٍ بلا وسوم", _CALL, True),
    ("JSON عارٍ داخل جملة", f"اكيد، {_CALL} ثانية وحدة", True),
    ("فراغات حول الـ JSON", f"[TOOL_CALL]  {_CALL}  ", True),
    ("رد طبيعي بلا استدعاء", "هلا بيك، شلون اكدر اساعدك؟", False),
    ("JSON بلا مفتاح tool", '{"order_id": "ORD-1001"}', False),
]


@pytest.mark.parametrize(
    "description,text,should_find",
    EXTRACT_CASES,
    ids=[c[0] for c in EXTRACT_CASES],
)
def test_extract_tool_call(description, text, should_find):
    call, _ = _extract_tool_call(text)
    if should_find:
        assert call is not None, description
        assert call["tool"] == "get_order_status", description
        assert call["args"]["order_id"] == "ORD-1001", description
    else:
        assert call is None, description


def test_visible_text_excludes_the_call():
    """النص المرئي المُعاد للسياق ما يحتوي وسوم الاستدعاء."""
    _, visible = _extract_tool_call(f"خليني اشوفلك [TOOL_CALL]{_CALL}")
    assert "TOOL_CALL" not in visible and visible == "خليني اشوفلك"


# --------------------------------------------------------------------------
# سلوك الحلقة كاملة (بموديل وهمي)
# --------------------------------------------------------------------------


class _FakeEngine:
    """محرك وهمي يرجّع ردوداً محضّرة بالتسلسل — بلا أي اتصال بـ vLLM."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def render_prompt(self, messages, tools=None):
        return messages

    async def generate_full(self, prompt, result_holder=None, **kwargs):
        self.calls += 1
        text = self._replies.pop(0) if self._replies else ""
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
    answer = _run([f"[TOOL_CALL]{_CALL}", "طلبك قيد التوصيل"], fake_engine)
    assert answer == "طلبك قيد التوصيل"


def test_call_without_closing_tag_still_executes(fake_engine):
    """بلا وسم ختامي (فما ينطلق الـ stop string): الاستدعاء لازم يُنفَّذ لا
    يُسلَّم نصاً خاماً — هذي كانت أكثر الانحرافات ضرراً."""
    answer = _run([f"[TOOL_CALL]{_CALL} لحظة", "طلبك قيد التوصيل"], fake_engine)
    assert answer == "طلبك قيد التوصيل"
    assert "TOOL_CALL" not in answer


def test_bare_json_call_still_executes(fake_engine):
    """JSON عارٍ بلا وسوم — الموديل نسى الوسوم وتذكّر البنية."""
    answer = _run([_CALL, "طلبك قيد التوصيل"], fake_engine)
    assert answer == "طلبك قيد التوصيل"


def test_malformed_call_is_not_leaked(fake_engine):
    """استدعاء مشوَّه تعذّر تفكيكه: يُحجب ويُستبدل برد آمن."""
    answer = _run(['[TOOL_CALL]{"tool": "get_order_status", "args": {'], fake_engine)
    assert answer == _EXHAUSTED_FALLBACK
    assert "TOOL_CALL" not in answer


def test_exhausted_rounds_return_fallback(fake_engine):
    """الموديل عالق يستدعي الأداة بكل جولة: العميل ياخذ رداً آمناً، مو آخر
    استدعاء أداة نصف مكتوب."""
    answer = _run([f"[TOOL_CALL]{_CALL}"] * 5, fake_engine, max_rounds=3)
    assert answer == _EXHAUSTED_FALLBACK
    assert "TOOL_CALL" not in answer


def test_plain_answer_passes_through(fake_engine):
    """رد عادي بلا أدوات يمر كما هو بجولة واحدة."""
    engine = fake_engine(["هلا بيك، شلون اساعدك؟"])
    answer = asyncio.run(
        run_with_tools([{"role": "user", "content": "هلا"}],
                       tools={"get_order_status": _ok_tool})
    )
    assert answer == "هلا بيك، شلون اساعدك؟"
    assert engine.calls == 1


def test_failing_tool_does_not_break_conversation(fake_engine):
    """أداة ترمي استثناء: الخطأ يُمرَّر للموديل ويكمل، بلا انهيار."""
    async def _boom(args):
        raise RuntimeError("الخدمة الخارجية واقعة")

    fake_engine([f"[TOOL_CALL]{_CALL}", "معذرة، صار خلل مؤقت"])
    answer = asyncio.run(
        run_with_tools([{"role": "user", "content": "وين طلبي"}],
                       tools={"get_order_status": _boom})
    )
    assert answer == "معذرة، صار خلل مؤقت"
