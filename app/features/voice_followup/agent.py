# -*- coding: utf-8 -*-
"""LiveKit agent worker — المكالمات الصوتية الحية (جيني للمتابعة، صباح للتأجيل).

التشغيل (يشغّله start.sh تلقائياً لو LIVEKIT_URL مضبوط):

    python -m app.features.voice_followup.agent download-files   # مرة وحدة: أوزان VAD
    python -m app.features.voice_followup.agent start

مسار المكالمة:
1. POST /voice_followup/calls (calls.py) ينشئ غرفة ويرسل هذا الـ agent إليها
   (dispatch صريح) مع بيانات الطلب كـ metadata.
2. الـ agent يتصل بالزبون عبر SIP لو LIVEKIT_SIP_TRUNK_ID مضبوط، وإلا ينتظر
   أول مشارك يدخل الغرفة (تطبيق سطح المكتب يربط صوت مكالمة الواتساب).
3. الحوار: Whisper ← النموذج (vLLM) ← Chatterbox، وكل قرار له ثمن تشغيلي يمر
   بأداة يتحقق منها الكود ثم يُرسل لباك اند البيانات (gateway.py).

**التزامن:** المكالمات تشتغل كخيوط بنفس العملية (JobExecutorType.THREAD)،
لحد `VOICE_MAX_CALLS` (افتراضياً 10) مكالمة متزامنة لكل worker:
  - النموذج: vLLM يخدم كل المكالمات سوية (continuous batching).
  - النطق: مجمّع من `TTS_POOL_SIZE` نسخة Chatterbox (tts.py)، كل نسخة لمكالمة بالمرة.
  - النسخ: Whisper نسخة وحدة؛ المقطع قصير فالانتظار عليه قليل.
الأوزان تُحمَّل مرة وحدة لكل العملية بدل نسخة لكل مكالمة.
"""

import logging
from typing import Literal, Optional

from livekit import api
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobExecutorType,
    JobProcess,
    RunContext,
    ToolError,
    cli,
    function_tool,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import openai, silero

from app.config import settings
from app.core.system_backend import SystemBackendUnavailable
from app.features.voice_followup import stt as whisper
from app.features.voice_followup import tts as chatterbox
from app.features.voice_followup.gateway import voice_followup_submitter, voice_postpone_submitter
from app.features.voice_followup.livekit_io import ChatterboxTTS, WhisperSTT
from app.features.voice_followup.postpone import (
    POSTPONE_CHOICES,
    option_label,
    postpone_days,
    resolve_postpone_date,
)
from app.features.voice_followup.prompts import build_followup_instructions, build_postpone_instructions
from app.features.voice_followup.schema import CallRequest, VoiceFollowupOrderRequest

logger = logging.getLogger(__name__)

CUSTOMER_IDENTITY = "customer"

# المخطط المرسَل للنموذج يحصر المفتاح بالجدول المولَّد من السقف (postpone.py).
PostponeChoice = Literal[POSTPONE_CHOICES]


def _customer_transcript(session: AgentSession) -> str:
    """كلام الزبون حرفياً كما نسخه Whisper، بترتيب المكالمة."""
    lines = []
    for item in session.history.items:
        if getattr(item, "role", None) == "user":
            text = getattr(item, "text_content", None)
            if text:
                lines.append(text)
    return "\n".join(lines)


def _end_call_tool() -> EndCallTool:
    # ignore_on_enter: ما ينقفل الخط أثناء التحية الأولى.
    return EndCallTool(delete_room=True, end_instructions=None, ignore_on_enter=True)


class FollowupAgent(Agent):
    """جيني: تسأل عن سبب حالة الطلب وتحفظ ملخّصه."""

    def __init__(self, order: VoiceFollowupOrderRequest) -> None:
        super().__init__(instructions=build_followup_instructions(order), tools=[_end_call_tool()])
        self._order = order
        self._saved = False

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions="ابدئي المكالمة: تحية قصيرة ثم اسألي عن سبب حالة الطلب.",
        )

    @function_tool
    async def save_reason(self, context: RunContext, summary: str) -> str:
        """Save the customer's reason for the order status. Call it once, after the customer explains.

        Args:
            summary: One short Arabic sentence summarizing the reason, using only the customer's own words.
        """
        if self._saved:
            return "السبب محفوظ مسبقاً. اشكري الزبون وأنهي المكالمة."
        summary = summary.strip()
        if not summary:
            raise ToolError("الملخّص فارغ. اكتبي جملة من كلام الزبون.")
        try:
            await voice_followup_submitter.submit(
                self._order, summary, _customer_transcript(context.session),
                settings.voice_followup_api_key,
            )
        except (SystemBackendUnavailable, Exception):
            logger.exception("فشل إرسال سبب الطلب %s لباك اند البيانات", self._order.order_id)
            raise ToolError("ما انحفظ السبب بسبب خلل بالنظام. اعتذري وگولي إن الفريق راح يتابع، ثم أنهي المكالمة.")
        self._saved = True
        return "انحفظ السبب. اشكري الزبون بجملة قصيرة وأنهي المكالمة."


class PostponeAgent(Agent):
    """صباح: تعرض التأجيل، تثبّت الموعد بأداتين (اقتراح ثم تأكيد)."""

    def __init__(self, order: VoiceFollowupOrderRequest) -> None:
        super().__init__(instructions=build_postpone_instructions(order), tools=[_end_call_tool()])
        self._order = order
        self._pending: Optional[str] = None
        self._confirmed = False

    async def on_enter(self) -> None:
        self.session.generate_reply(
            instructions="ابدئي المكالمة: التعريف، ثم الشحنة بسطر واحد، ثم اسألي: اليوم لو باجر لو بعد باجر؟",
        )

    @function_tool
    async def propose_postpone(self, context: RunContext, choice: PostponeChoice) -> str:
        """Register the delivery date the customer chose, before confirming it with them.

        Args:
            choice: The date key: "today", "plus_N" (N days from today, 1-14) or "weekday_D" (0=Monday ... 6=Sunday).
        """
        if choice not in POSTPONE_CHOICES:
            raise ToolError("موعد غير مسموح. أقصى تأجيل أسبوعين؛ اسألي الزبون عن موعد أقرب.")
        self._pending = choice
        label = option_label(choice, postpone_days(choice))
        return f"الموعد: {label} ({resolve_postpone_date(choice)}). أكّدي «{label}» مع الزبون وانتظري جوابه."

    @function_tool
    async def confirm_postpone(self, context: RunContext) -> str:
        """Save the proposed date after the customer explicitly confirmed it (yes)."""
        if self._confirmed:
            return "الموعد محفوظ مسبقاً. اختمي المكالمة."
        if self._pending is None:
            raise ToolError("ماكو موعد مقترح. استدعي propose_postpone أولاً بعد ما يحدد الزبون موعده.")
        new_date = resolve_postpone_date(self._pending)
        try:
            await voice_postpone_submitter.submit(
                self._order, new_date, self._pending, settings.voice_followup_api_key,
            )
        except (SystemBackendUnavailable, Exception):
            logger.exception("فشل إرسال تأجيل الطلب %s لباك اند البيانات", self._order.order_id)
            raise ToolError("ما انحفظ الموعد بسبب خلل بالنظام. اعتذري وگولي إن الفريق راح يعاود الاتصال، ثم أنهي المكالمة.")
        self._confirmed = True
        return "انحفظ الموعد. اشكري الزبون بجملة قصيرة وأنهي المكالمة."


def _llm() -> openai.LLM:
    """نفس خادم vLLM المستخدم بباقي الخدمة، بلا تفكير لأن زمن الرد بالمكالمة حساس."""
    return openai.LLM(
        model=settings.model_name,
        base_url=settings.vllm_base_url,
        api_key="not-needed",
        temperature=0.0,
        extra_body={"reasoning_effort": "none", "chat_template_kwargs": {"enable_thinking": False}},
        # vLLM/LM Studio ما يعتمدون strict بمخطط الأدوات.
        _strict_tool_schema=False,
    )


def build_agent(request: CallRequest) -> Agent:
    if request.call_type == "postpone":
        return PostponeAgent(request.order)
    return FollowupAgent(request.order)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()
    # الأوزان عامة على مستوى العملية؛ أول خيط يحمّلها والباقي يعيد استخدامها.
    whisper.warmup()
    chatterbox.warmup()


def _call_load(server: AgentServer) -> float:
    """حمل الـ worker = المكالمات الجارية ÷ الحد الأقصى. LiveKit يوقف توجيه
    مكالمات جديدة لهذا الـ worker لما يوصل الحد (بدل حساب الحمل من CPU)."""
    return len(server.active_jobs) / settings.voice_max_concurrent_calls


server = AgentServer(
    job_executor_type=JobExecutorType.THREAD,
    load_fnc=_call_load,
    # يقبل لحد ما يصير الحمل 1.0 بالضبط (= الحد الأقصى من المكالمات).
    load_threshold=0.99,
    ws_url=settings.livekit_url or None,
    api_key=settings.livekit_api_key or None,
    api_secret=settings.livekit_api_secret or None,
    setup_fnc=prewarm,
)


@server.rtc_session(agent_name=settings.livekit_agent_name)
async def entrypoint(ctx: JobContext) -> None:
    request = CallRequest.model_validate_json(ctx.job.metadata)
    order = request.order
    logger.info("مكالمة %s للطلب %s بالغرفة %s", request.call_type, order.order_id, ctx.room.name)

    await ctx.connect()
    if settings.livekit_sip_trunk_id and order.customer_phone:
        try:
            await ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=settings.livekit_sip_trunk_id,
                sip_call_to=order.customer_phone,
                participant_identity=CUSTOMER_IDENTITY,
                wait_until_answered=True,
            ))
        except api.TwirpError as exc:
            logger.warning("ما انرد على مكالمة الطلب %s: %s", order.order_id, exc.message)
            ctx.shutdown("customer did not answer")
            return
        participant = await ctx.wait_for_participant(identity=CUSTOMER_IDENTITY)
    else:
        # بلا SIP: تطبيق سطح المكتب يدخل الغرفة ويربط صوت مكالمة الواتساب.
        participant = await ctx.wait_for_participant()

    session = AgentSession(
        stt=WhisperSTT(),
        llm=_llm(),
        tts=ChatterboxTTS(),
        vad=ctx.proc.userdata["vad"],
    )
    await session.start(room=ctx.room, agent=build_agent(request))
    logger.info("بدأت المكالمة مع %s", participant.identity)


if __name__ == "__main__":
    cli.run_app(server)
