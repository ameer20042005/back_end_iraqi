# -*- coding: utf-8 -*-
"""LiveKit agent worker — المكالمات الصوتية الحية العامة.

التشغيل (يشغّله start.sh تلقائياً لو LIVEKIT_URL مضبوط):

    python -m app.features.voice_followup.agent download-files   # مرة وحدة: أوزان VAD
    python -m app.features.voice_followup.agent start

مسار المكالمة:
1. POST /voice_followup/calls (calls.py) ينشئ غرفة ويرسل هذا الـ agent إليها
   (dispatch صريح) مع هدف المكالمة كـ metadata (AgentCallMetadata، بلا اسم الزبون).
2. الـ agent يتصل بالزبون عبر SIP لو LIVEKIT_SIP_TRUNK_ID مضبوط، وإلا ينتظر
   أول مشارك يدخل الغرفة (تطبيق سطح المكتب يربط صوت مكالمة الواتساب).
3. الحوار: Whisper ← النموذج (vLLM) ← Chatterbox. النتيجة تمر بأداة save_outcome:
   تنحفظ بملف (outcomes.py) يسحبه التطبيق، وتُرسل لباك اند السستم إن كان مضبوطاً.
   منطق الحفظ مشترك مع الوكيل المحلي عبر WebSocket (call_tools.py).

**التزامن:** المكالمات تشتغل كخيوط بنفس العملية (JobExecutorType.THREAD)،
لحد `VOICE_MAX_CALLS` (افتراضياً 10) مكالمة متزامنة لكل worker:
  - النموذج: vLLM يخدم كل المكالمات سوية (continuous batching).
  - النطق: مجمّع من `TTS_POOL_SIZE` نسخة Chatterbox (tts.py)، كل نسخة لمكالمة بالمرة.
  - النسخ: Whisper نسخة وحدة؛ المقطع قصير فالانتظار عليه قليل.
الأوزان تُحمَّل مرة وحدة لكل العملية بدل نسخة لكل مكالمة.
"""

import logging
from typing import List, Optional

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
from app.features.voice_followup import call_tools, outcomes  # noqa: F401  (outcomes: للاختبارات)
from app.features.voice_followup import stt as whisper
from app.features.voice_followup import tts as chatterbox
from app.features.voice_followup.gateway import call_outcome_submitter
from app.features.voice_followup.livekit_io import ChatterboxTTS, WhisperSTT
from app.features.voice_followup.prompts import build_call_instructions, build_opening_instruction
from app.features.voice_followup.schema import AgentCallMetadata, OutcomeAnswer

logger = logging.getLogger(__name__)

CUSTOMER_IDENTITY = "customer"
NO_ANSWER_SUMMARY = call_tools.NO_ANSWER_SUMMARY
NO_OUTCOME_SUMMARY = call_tools.NO_OUTCOME_SUMMARY


def _customer_transcript(session: Optional[AgentSession]) -> str:
    """كلام الزبون حرفياً كما نسخه Whisper، بترتيب المكالمة."""
    if session is None:
        return ""
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


def _registry_tools(meta: AgentCallMetadata, room: str) -> list:
    """أدوات البيانات من سجل call_tools (نفس تنفيذ مسار WebSocket) كأدوات LiveKit
    خام. save_outcome/end_call لهن تنفيذ LiveKit الخاص تحت."""
    ctx = call_tools.CallToolContext(room=room, meta=meta)
    tools = []
    for tool in call_tools.active_tools(exclude={call_tools.SAVE_OUTCOME, call_tools.END_CALL}):
        async def handler(raw_arguments: dict, _name: str = tool.name) -> str:
            return await call_tools.run_tool(ctx, _name, raw_arguments)

        tools.append(function_tool(handler, raw_schema={
            "name": tool.name, "description": tool.description, "parameters": tool.parameters,
        }))
    return tools


class CallAgent(Agent):
    """موظف مبيعات/خدمة عملاء عام: يحقق هدف الموظف ويحفظ النتيجة مرة وحدة."""

    def __init__(self, meta: AgentCallMetadata, room: str) -> None:
        super().__init__(
            instructions=build_call_instructions(meta, call_tools.data_tools_enabled()),
            tools=[_end_call_tool(), *_registry_tools(meta, room)],
        )
        self._meta = meta
        self._room = room
        self.saved = False

    async def on_enter(self) -> None:
        self.session.generate_reply(instructions=build_opening_instruction(self._meta))

    @function_tool
    async def save_outcome(
        self,
        context: RunContext,
        summary: str,
        answers: Optional[List[OutcomeAnswer]] = None,
        follow_up_needed: bool = False,
    ) -> str:
        """Save the call outcome once the goal is achieved or the customer declines. Call it only once.

        Args:
            summary: One or two short Arabic sentences summarizing only what the customer actually said.
            answers: The asked questions with the customer's answer to each, in the customer's own words.
            follow_up_needed: True if the customer asked for something the team must follow up on.
        """
        if self.saved:
            return "النتيجة محفوظة مسبقاً. اشكر الزبون وأنهِ المكالمة."
        try:
            await call_tools.store_outcome(
                self._meta, self._room, summary, answers, follow_up_needed,
                _customer_transcript(context.session), submitter=call_outcome_submitter,
            )
        except call_tools.CallToolError as exc:
            raise ToolError(str(exc)) from exc
        self.saved = True
        return "انحفظت النتيجة. اشكر الزبون بجملة قصيرة وأنهِ المكالمة."


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


def build_agent(meta: AgentCallMetadata, room: str) -> CallAgent:
    return CallAgent(meta, room)


def save_fallback_outcome(meta: AgentCallMetadata, room: str, summary: str,
                          session: Optional[AgentSession] = None) -> None:
    """مكالمة انتهت بلا save_outcome: نفس منطق call_tools.save_fallback_outcome."""
    call_tools.save_fallback_outcome(meta, room, summary, _customer_transcript(session))


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
    meta = AgentCallMetadata.model_validate_json(ctx.job.metadata)
    room = ctx.room.name
    logger.info("مكالمة %s بالغرفة %s", meta.reference_id or "-", room)

    await ctx.connect()
    if settings.livekit_sip_trunk_id and meta.phone:
        try:
            await ctx.api.sip.create_sip_participant(api.CreateSIPParticipantRequest(
                room_name=room,
                sip_trunk_id=settings.livekit_sip_trunk_id,
                sip_call_to=meta.phone,
                participant_identity=CUSTOMER_IDENTITY,
                wait_until_answered=True,
            ))
        except api.TwirpError as exc:
            logger.warning("ما انرد على مكالمة الغرفة %s: %s", room, exc.message)
            save_fallback_outcome(meta, room, NO_ANSWER_SUMMARY)
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
    agent = build_agent(meta, room)

    async def _on_shutdown(_reason: str) -> None:
        if not agent.saved:
            save_fallback_outcome(meta, room, NO_OUTCOME_SUMMARY, session)

    ctx.add_shutdown_callback(_on_shutdown)
    await session.start(room=ctx.room, agent=agent)
    logger.info("بدأت المكالمة مع %s", participant.identity)


if __name__ == "__main__":
    cli.run_app(server)
