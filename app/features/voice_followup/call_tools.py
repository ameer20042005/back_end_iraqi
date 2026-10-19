# -*- coding: utf-8 -*-
"""Shared business tools for the Pipecat call agent and persisted call outcomes."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional

from pydantic import ValidationError

from app.config import settings
from app.core import catalog
from app.features.voice_followup import gateway, outcomes
from app.features.voice_followup.schema import AgentCallMetadata, CallOutcome, OutcomeAnswer

logger = logging.getLogger(__name__)

NO_ANSWER_SUMMARY = "الزبون لم يرد على المكالمة"
NO_OUTCOME_SUMMARY = "انتهت المكالمة بدون نتيجة مسجّلة"

SAVE_OUTCOME = "save_outcome"
END_CALL = "end_call"


class CallToolError(Exception):
    """فشل متوقَّع؛ رسالته ترجع للنموذج كنتيجة الأداة حتى يتصرف بالمكالمة."""


# ---------------------------------------------------------------------------
# منطق النتيجة المشترك (Pipecat WebSocket)
# ---------------------------------------------------------------------------

def _parse_answers(answers: Optional[Iterable[Any]]) -> List[OutcomeAnswer]:
    parsed = []
    for item in answers or []:
        try:
            answer = OutcomeAnswer.model_validate(item)
        except ValidationError:
            continue
        if answer.question.strip() and answer.answer.strip():
            parsed.append(answer)
    return parsed


async def store_outcome(
    meta: AgentCallMetadata,
    room: str,
    summary: Any,
    answers: Optional[Iterable[Any]],
    follow_up_needed: Any,
    customer_transcript: str,
    submitter: Optional[gateway.CallOutcomeSubmitter] = None,
) -> CallOutcome:
    """يحفظ النتيجة بالمخزن (المصدر اللي يسحبه التطبيق) ثم يرسلها لباك اند
    السستم best effort. يرفع CallToolError لو الملخّص فارغ أو فشل الحفظ."""
    summary = summary.strip() if isinstance(summary, str) else ""
    if not summary:
        raise CallToolError("الملخّص فارغ. اكتب جملة من كلام الزبون.")
    outcome = CallOutcome(
        room=room,
        status="done",
        reference_id=meta.reference_id,
        summary=summary,
        answers=_parse_answers(answers),
        follow_up_needed=bool(follow_up_needed),
        customer_transcript=customer_transcript,
        ended_at=outcomes.now_iso(),
    )
    try:
        outcomes.save_outcome(outcome)
    except Exception as exc:
        logger.exception("فشل حفظ نتيجة المكالمة %s", room)
        raise CallToolError(
            "ما انحفظت النتيجة بسبب خلل بالنظام. اعتذر وگول إن الفريق راح يتابع، ثم أنهِ المكالمة."
        ) from exc
    try:
        async with asyncio.timeout(settings.call_outcome_submit_timeout_seconds):
            await (submitter or gateway.call_outcome_submitter).submit(outcome, settings.voice_followup_api_key)
    except Exception:
        # النتيجة محفوظة محلياً والتطبيق يسحبها؛ الإرسال لباك اند السستم إضافي.
        logger.warning("تعذّر إرسال نتيجة المكالمة %s لباك اند السستم", room, exc_info=True)
    return outcome


def save_fallback_outcome(meta: AgentCallMetadata, room: str, summary: str,
                          customer_transcript: str = "") -> Optional[CallOutcome]:
    """مكالمة انتهت بلا save_outcome (ما رد، رقم غلط، انقطع الخط): نتيجة done
    حتى ما يبقى التطبيق ينتظر pending للأبد. ما تكتب فوق نتيجة محفوظة."""
    try:
        if outcomes.load_outcome(room) is not None:
            return None
        outcome = CallOutcome(
            room=room, status="done", reference_id=meta.reference_id, summary=summary,
            follow_up_needed=True, customer_transcript=customer_transcript,
            ended_at=outcomes.now_iso(),
        )
        outcomes.save_outcome(outcome)
        return outcome
    except Exception:
        logger.exception("فشل حفظ نتيجة احتياطية للغرفة %s", room)
        return None


# ---------------------------------------------------------------------------
# سجل الأدوات (مسار WebSocket)
# ---------------------------------------------------------------------------

@dataclass
class CallToolContext:
    """حالة المكالمة اللي تشوفها الأدوات. ما بيها اسم الزبون أبداً."""

    room: str
    meta: AgentCallMetadata
    customer_transcript: Callable[[], str] = lambda: ""
    saved: bool = False
    end_requested: bool = False
    outcome: Optional[CallOutcome] = None
    # مكان حر لأدوات لاحقة تحتاج تحفظ شي بين استدعاءاتها بنفس المكالمة.
    extra: Dict[str, Any] = field(default_factory=dict)


ToolHandler = Callable[[CallToolContext, Dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class CallTool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: ToolHandler
    # الأداة تنعرض للنموذج بس لما يرجع True (مثلاً: إعداد باك اند البيانات موجود).
    enabled: Callable[[], bool] = lambda: True

    def openai_schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters,
        }}


REGISTRY: Dict[str, CallTool] = {}


def register(tool: CallTool) -> CallTool:
    REGISTRY[tool.name] = tool
    return tool


def active_tools(exclude: Iterable[str] = ()) -> List[CallTool]:
    skip = set(exclude)
    return [t for name, t in REGISTRY.items() if name not in skip and t.enabled()]


def openai_tools(exclude: Iterable[str] = ()) -> List[dict]:
    return [t.openai_schema() for t in active_tools(exclude)]


def data_tools_enabled() -> bool:
    return any(t.name in DATA_TOOLS for t in active_tools())


async def run_tool(ctx: CallToolContext, name: str, arguments: Any) -> str:
    """ينفّذ أداة من السجل ويرجع نتيجتها نصاً للنموذج — أي فشل يصير رسالة."""
    tool = REGISTRY.get(name)
    if tool is None or not tool.enabled():
        return f"الأداة {name} غير موجودة."
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            return "وسائط الأداة مو JSON صالح."
    if not isinstance(arguments, dict):
        return "وسائط الأداة لازم تكون كائن JSON."
    try:
        return await tool.handler(ctx, arguments)
    except CallToolError as exc:
        return str(exc)
    except Exception:
        logger.exception("فشل تنفيذ الأداة %s بالمكالمة %s", name, ctx.room)
        return "صار خلل بالنظام. اعتذر وگول إن الفريق راح يتابع."


async def _save_outcome(ctx: CallToolContext, args: Dict[str, Any]) -> str:
    if ctx.saved:
        return "النتيجة محفوظة مسبقاً. اشكر الزبون وأنهِ المكالمة."
    ctx.outcome = await store_outcome(
        ctx.meta, ctx.room, args.get("summary"), args.get("answers"),
        args.get("follow_up_needed", False), ctx.customer_transcript(),
    )
    ctx.saved = True
    return "انحفظت النتيجة. النظام سيشكر الزبون وينهي المكالمة تلقائياً."


async def _end_call(ctx: CallToolContext, args: Dict[str, Any]) -> str:
    ctx.end_requested = True
    return "المكالمة راح تنتهي بعد جملتك الأخيرة."


register(CallTool(
    name=SAVE_OUTCOME,
    description=(
        "Finish the call by saving the outcome once all requested information is collected, "
        "or the customer declines. The server says goodbye and hangs up automatically after success. "
        "Never call before hearing the customer. Summarize only actual customer answers."
    ),
    parameters={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "One or two short Arabic sentences summarizing only what the customer actually said.",
            },
            "answers": {
                "type": "array",
                "description": "The asked questions with the customer's answer to each, in the customer's own words.",
                "items": {
                    "type": "object",
                    "properties": {"question": {"type": "string"}, "answer": {"type": "string"}},
                    "required": ["question", "answer"],
                },
            },
            "follow_up_needed": {
                "type": "boolean",
                "description": "True if the customer asked for something the team must follow up on.",
            },
        },
        "required": ["summary"],
    },
    handler=_save_outcome,
))

register(CallTool(
    name=END_CALL,
    description=(
        "End the call. Say your short closing sentence in the same reply first; "
        "the line is closed right after it is spoken."
    ),
    parameters={"type": "object", "properties": {}},
    handler=_end_call,
))


# ---------------------------------------------------------------------------
# أدوات البيانات (Catalog)
# ---------------------------------------------------------------------------

GET_MY_ORDERS = "get_my_orders"
GET_ORDER_STATUS = "get_order_status"
SEARCH_PRODUCTS = "search_products"
DATA_TOOLS = (GET_MY_ORDERS, GET_ORDER_STATUS, SEARCH_PRODUCTS)

_MAX_ORDERS = 5
NOT_FOUND_ORDER = "ماكو طلب بهذا الرقم لهذا الزبون. گوله بأدب وما تخمّن."
_UNAVAILABLE = "ما أگدر أوصل لبيانات المتجر هسه. گول للزبون إن الفريق راح يتابع وياه."


def _json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


async def _get_my_orders(ctx: CallToolContext, args: Dict[str, Any]) -> str:
    phone = ctx.meta.phone
    if not catalog.phone_digits(phone):
        return "رقم الزبون غير معروف بهذه المكالمة، فما أگدر أجيب طلباته. گول إن الفريق راح يتابع."
    try:
        orders = await catalog.get_customer_orders(phone)
    except catalog.CatalogError as exc:
        logger.warning("get_my_orders فشل بالمكالمة %s: %s", ctx.room, exc)
        raise CallToolError(_UNAVAILABLE) from exc
    if not orders:
        return "ماكو طلبات مسجّلة لهذا الزبون."
    return _json({"count": len(orders), "orders": [catalog.trim_order(o) for o in orders[:_MAX_ORDERS]]})


async def _get_order_status(ctx: CallToolContext, args: Dict[str, Any]) -> str:
    order_id = str(args.get("order_id") or "").strip()
    if not order_id.isdigit() or not catalog.phone_digits(ctx.meta.phone):
        return NOT_FOUND_ORDER
    try:
        order = await catalog.get_order(order_id)
    except catalog.CatalogError as exc:
        if exc.reason in ("not_found", "bad_request"):
            return NOT_FOUND_ORDER
        logger.warning("get_order_status فشل بالمكالمة %s: %s", ctx.room, exc)
        raise CallToolError(_UNAVAILABLE) from exc
    # الملكية: طلب زبون ثاني = "غير موجود" (ما نكشف إنه موجود أصلاً).
    if not catalog.same_phone(order.get("customerPhoneNumber"), ctx.meta.phone):
        return NOT_FOUND_ORDER
    return _json(catalog.trim_order(order))


async def _search_products(ctx: CallToolContext, args: Dict[str, Any]) -> str:
    text = str(args.get("text") or "").strip()[:200]
    if not text:
        raise CallToolError("اكتب اسم المنتج اللي يسأل عنه الزبون.")
    try:
        products = await catalog.search_products_with_stock(text)
    except catalog.CatalogError as exc:
        logger.warning("search_products فشل بالمكالمة %s: %s", ctx.room, exc)
        raise CallToolError(_UNAVAILABLE) from exc
    if not products:
        return "ماكو منتج مطابق بالمتجر. گول للزبون وما تخترع منتجات."
    return _json({"count": len(products), "products": products})


register(CallTool(
    name=GET_MY_ORDERS,
    description=(
        "List the current customer's recent orders with their status. The customer is identified "
        "automatically from the call; never ask for a phone number."
    ),
    parameters={"type": "object", "properties": {}},
    handler=_get_my_orders,
    enabled=catalog.configured,
))

register(CallTool(
    name=GET_ORDER_STATUS,
    description="Get the status of one of the current customer's orders by its order number.",
    parameters={
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "The order number the customer said (digits)."}},
        "required": ["order_id"],
    },
    handler=_get_order_status,
    enabled=catalog.configured,
))

register(CallTool(
    name=SEARCH_PRODUCTS,
    description=(
        "Search the store catalog for a product the customer asks about. Returns real products with "
        "price and availability. Never mention products that are not in the result."
    ),
    parameters={
        "type": "object",
        "properties": {"text": {"type": "string", "description": "The product as the customer said it."}},
        "required": ["text"],
    },
    handler=_search_products,
    enabled=catalog.configured,
))
