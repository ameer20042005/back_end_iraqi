# -*- coding: utf-8 -*-
"""صيغ المكالمات الصوتية العامة: طلب بدء مكالمة، ونتيجتها، وما يصل للـ agent.

المكالمة بلا سيناريو ثابت: هدفها (`goal`) وأسئلتها يكتبها موظف تطبيق سطح
المكتب، واسم الشركة من إعدادات التطبيق يُرسل بكل طلب.

الخصوصية: اسم الزبون ما يدخل metadata الـ agent أصلاً (AgentCallMetadata)،
فما يوصل لسياق نموذج المكالمة."""

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints


def _text(min_length: int = 0, max_length: Optional[int] = None):
    return Annotated[str, StringConstraints(strip_whitespace=True, min_length=min_length, max_length=max_length)]


class Contact(BaseModel):
    # الاسم للتطبيق فقط — ما يدخل سياق نموذج المكالمة أبداً.
    name: Optional[_text(max_length=120)] = None
    # لازم لوضع sip؛ بلاه المكالمة bridge (التطبيق يربط صوت الواتساب).
    phone: Optional[_text(max_length=32)] = None


class CallRequest(BaseModel):
    """جسم POST /voice_followup/calls — وهو نفسه وسائط أداة start_call اللي
    يرجعها مساعد الموظف (التطبيق يملأ company_name من إعداداته)."""

    company_name: _text(1, 120)
    agent_name: Optional[_text(max_length=60)] = None
    goal: _text(1, 1000)
    questions: List[_text(1, 300)] = Field(default_factory=list, max_length=10)
    context: Optional[_text(max_length=2000)] = None
    contact: Contact = Field(default_factory=Contact)
    reference_id: Optional[_text(max_length=120)] = None
    language: _text(2, 8) = "ar"


class AgentCallMetadata(BaseModel):
    """ما يصل للـ agent عبر metadata الـ dispatch — CallRequest بلا اسم الزبون."""

    company_name: str
    agent_name: Optional[str] = None
    goal: str
    questions: List[str] = Field(default_factory=list)
    context: Optional[str] = None
    reference_id: Optional[str] = None
    language: str = "ar"
    # رقم الهاتف للاتصال عبر SIP فقط؛ ما يدخل تعليمات النموذج.
    phone: Optional[str] = None


class CallResponse(BaseModel):
    room: str
    # Deprecated compatibility keys for existing desktop IPC consumers.
    # Pipecat uses stream_path/stream_id; these keys never enable an RTC path.
    livekit_url: str = Field(default="", deprecated=True)
    # توكن دخول الغرفة لتطبيق سطح المكتب (مراقبة المكالمة أو تسلّمها من الموظف).
    operator_token: str = Field(default="", deprecated=True)
    dispatch_id: str = Field(default="", deprecated=True)
    # "sip": الـ agent يتصل بالزبون بنفسه عبر SIP trunk.
    # "bridge": التطبيق يدخل الغرفة بـ operator_token ويربط صوت مكالمة الواتساب.
    dial_mode: Literal["sip", "bridge"]
    transport: Optional[Literal["pipecat"]] = None
    stream_path: Optional[str] = None
    stream_id: Optional[str] = None
    expires_in_seconds: Optional[int] = None


class OutcomeAnswer(BaseModel):
    question: str
    answer: str


class CallOutcome(BaseModel):
    """جواب GET /voice_followup/calls/{room}/outcome."""

    room: str
    status: Literal["pending", "done"]
    reference_id: Optional[str] = None
    summary: Optional[str] = None
    answers: List[OutcomeAnswer] = Field(default_factory=list)
    follow_up_needed: bool = False
    customer_transcript: Optional[str] = None
    ended_at: Optional[str] = None
