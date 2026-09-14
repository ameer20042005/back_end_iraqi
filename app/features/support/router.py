# -*- coding: utf-8 -*-
"""دعم العملاء: POST /support/chat — تتبع حالة الطلب برقم الطلب أو الهاتف."""

import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from functools import partial
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError

from app import sessions
from app.auth import require_support_api_key
from app.engine import llm_engine
from app.features.support.prompts import build_support_prompt, build_support_query_prompt
from app.intent_router import is_pure_chitchat
from app.order_gateway import order_status_provider
from app.order_query import OrderQuery, PagedOrders
from app.system_backend import SystemBackendUnavailable, caller_auth_token
from app.text_norm import normalize
from app.tool_loop import (
    EXHAUSTED_FALLBACK,
    answer_without_tools,
    run_decision_rounds,
    run_with_tools,
    stream_final_answer,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/support", tags=["support"])

_SESSION_PREFIX = "support:"
_ORDER_ID_RE = re.compile(r"ORD[\s\-_]*(\d+)", re.IGNORECASE)

# تاريخ صريح بالرسالة: ISO (٢٠٢٦-٠٨-٠١) أو يوم/شهر/سنة بالعرف العراقي
# (١/٨/٢٠٢٦). الفاصل "-" أو "/" — نفس نمط استخراج رقم الطلب/الهاتف أعلاه:
# نطبّع أولاً (أرقام لاتينية) ثم نلتقط الصيغة بـregex.
_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DMY_DATE_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b")
_ANY_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/-]\d{1,2}[/-]\d{4}")


def _extract_explicit_dates(message: str) -> List[str]:
    """يرجع كل التواريخ الصريحة المذكورة بالرسالة، محوّلة لصيغة ISO
    "YYYY-MM-DD" — يقبل ISO مباشرة أو يوم/شهر/سنة. تاريخ غير صالح فعلياً
    (مثل ٣٢/١٣/٢٠٢٦) يُتجاهل بصمت بدل ما يكسر باقي الرسالة."""
    normalized = normalize(message, keep_punctuation=True)
    found: List[str] = []
    for token in _ANY_DATE_RE.findall(normalized):
        iso_match = _ISO_DATE_RE.fullmatch(token)
        if iso_match:
            year, month, day = iso_match.groups()
        else:
            dmy_match = _DMY_DATE_RE.fullmatch(token)
            if not dmy_match:
                continue
            day, month, year = dmy_match.groups()
        try:
            found.append(date(int(year), int(month), int(day)).isoformat())
        except ValueError:
            continue
    return found


# الفترات النسبية المسموحة — **مفاتيح مغلقة، لا نصوص حرة**. النموذج يختار
# مفتاحاً والكود يحسب التاريخين. نفس مبدأ POSTPONE_CHOICES بمكالمة التأجيل:
# لو خلّينا النموذج يرجّع تاريخاً جاهزاً، صار عندنا تاريخ من نموذج يدخل
# استعلاماً تشغيلياً ما نكدر نتحقق منه — بينما المفتاح نتحقق منه بسطر واحد.
RELATIVE_RANGES = ("today", "yesterday", "last_week", "last_month", "none")


def resolve_relative_range(key: str, today: Optional[date] = None) -> Optional[Tuple[str, str]]:
    """يحوّل مفتاح فترة نسبية لـ(date_from, date_to) بصيغة ISO، أو None
    للمفتاح "none" أو أي مفتاح مجهول.

    `today` قابلة للتمرير للاختبار (تاريخ ثابت بدل تاريخ التشغيل الفعلي)."""
    today = today or date.today()

    if key == "today":
        return today.isoformat(), today.isoformat()
    if key == "yesterday":
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat()
    if key == "last_week":
        # آخر ٧ أيام قبل اليوم — تعريف عملي بسيط، لا تقويم أسبوعي رسمي
        # (السبت-الجمعة مثلاً).
        end = today - timedelta(days=1)
        return (end - timedelta(days=6)).isoformat(), end.isoformat()
    if key == "last_month":
        first_of_this_month = today.replace(day=1)
        last_of_prev = first_of_this_month - timedelta(days=1)
        return last_of_prev.replace(day=1).isoformat(), last_of_prev.isoformat()
    return None


def extract_date_range(message: str, relative_key: str = "none") -> Optional[Tuple[str, str]]:
    """يحسب فترة (date_from, date_to) بصيغة ISO — يغذّي فلترة
    _bulk_query_answer المحلية عبر created_at_in_range (app/order_gateway.py).

    الترتيب: (١) تاريخان صريحان بالرسالة → أصغرهما date_from وأكبرهما
    date_to بغض النظر عن ترتيب ذكرهما. (٢) تاريخ صريح واحد بس → يُعتبر
    يوماً واحداً. (٣) مفتاح الفترة النسبية اللي فهمه النموذج.

    التواريخ الصريحة تبقى تُستخرج بـregex لا بالنموذج: «2026-08-01» صيغة
    لا لغة — نفس مبرر إبقاء _PHONE_RE و_ORDER_ID_RE أدناه. الفرق أن
    «الشهر الماضي» **لغة**، وهي اللي انتقلت للنموذج."""
    explicit = _extract_explicit_dates(message)
    if len(explicit) >= 2:
        ordered = sorted(explicit)
        return ordered[0], ordered[-1]
    if len(explicit) == 1:
        return explicit[0], explicit[0]
    return resolve_relative_range(relative_key)

# أرقام الهواتف العراقية: 07XXXXXXXXX (11 خانة). الزبون يكتبها بصيغ كثيرة —
# بأرقام عربية-هندية، بفواصل/شرطات، بمقدمة دولية (+964 / 00964 / 964) اللي
# تستبدل الصفر الأول. نطبّع أولاً بالطبقة ١ (أرقام لاتينية) ثم نلتقط الصيغ.
_PHONE_RE = re.compile(
    r"(?:(?:\+|00)?964[\s\-]*)?0?7[\s\-]*(\d[\s\-]*){9}"
)
_NON_DIGITS_RE = re.compile(r"\D")


class SupportChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    # توكن JWT الخاص بالمستخدم الأصلي، يرسله jbot (الوسيط) مع كل رسالة. ما
    # نفكّه ولا نفهمه هنا — نمرره فقط لباك اند السستم بترويسة Authorization
    # (انظر app/system_backend.py::caller_auth_token) حتى يرجّع بيانات شركة
    # هذا المستخدم تحديداً. اختياري: عميل يستدعي /support/chat مباشرة بلا
    # وسيط يبقى يشتغل كما كان.
    auth_token: Optional[str] = None


class SupportChatResponse(BaseModel):
    session_id: str
    answer: str
    engine: str
    # سجل استدعاءات الأدوات (get_order_status) — للشفافية فقط، نفس مبرر
    # SalesChatResponse.tool_calls (app/features/sales/router.py). فارغة
    # بالمسارات الحتمية (_deterministic_status_answer) لأنها ما تمر بحلقة
    # الأدوات أصلاً — تُملأ فقط لما الموديل+الأداة هو من جاوب.
    tool_calls: List[dict] = []


async def _list_all_cached(session_id: str, api_key: str) -> List[dict]:
    """list_all() مع كاش بحدود الجلسة وبـTTL — انظر sessions.cached_orders().

    لم يعد يُحقن بالبرومبت (docs/fix-plan.md § المرحلة 4). مستهلكه الوحيد
    الآن اشتقاق الحالات/المندوبين الموجودين فعلاً بالبيانات (_known_statuses/
    _known_transporters) — قد يحتاجهما المخطط الحي وبناء الاستعلام ضمن الرسالة
    الواحدة، والكاش يمتص التكرار داخل الدقيقة. يبقى لحين ما يوفّر باك اند
    السستم مساراً لقائمة الحالات (العطل B9)."""
    cached = sessions.cached_orders(session_id)
    if cached is not None:
        return cached
    orders = await order_status_provider.list_all(api_key)
    sessions.cache_orders(session_id, orders)
    return orders


_QUERY_ARGS_HELP = (
    "المسموح: phone, order_id, status, city, customer_name, date_from, date_to, "
    "limit (1-50), offset"
)


def _parse_query_args(args: dict) -> OrderQuery:
    """يبني OrderQuery من args التي أرسلها الموديل. التمرير عبر النموذج لا
    استعمال args مباشرة: أي معامل مخترَع يُرفَض هنا (ValidationError) برسالة
    واضحة للموديل فيصحّح نفسه بالجولة التالية — بدل ما يُتجاهَل صامتاً ويظن
    فلتره مطبَّقاً. "all": true من البرومبت القديم يُهمَل بتسامح (كان يعني
    "بلا معايير" وهذا هو الافتراضي أصلاً)."""
    return OrderQuery(**{k: v for k, v in args.items() if k != "all"})


def _invalid_args_error(exc: ValidationError) -> dict:
    # ValidationError تُلتقط وتُرجَع كنص عربي: رد الأداة يدخل سياق الموديل،
    # فلازم يكون مفهوماً له لا stack trace.
    return {"error": f"معاملات غير صالحة: {exc.error_count()} حقل — {_QUERY_ARGS_HELP}"}


async def _get_order_status_tool(args: dict, api_key: str, session_id: str = "") -> dict:
    """أداة الموديل — بحث مصفّح بمعايير مركّبة (app/order_query.py).

    الموديل ليس محرك بحث؛ هو **مترجم** بين كلام الموظف والمعايير. البحث
    نفسه يصير بالمستودع/باك اند السستم ويرجع صفحة صغيرة + العدد الكلي
    (docs/fix-plan.md § المرحلة 3 — يعالج B1 · B2).

    الحقول بالرد مقصودة كلها:
      showing — كم وصل فعلاً (الموديل يعدّ بصرياً بشكل رديء، نعطيه الرقم)
      total   — كم موجود بالنظام ← الحقل الحاسم، هو إصلاح العطل B1
      hint    — تعليمة عربية صريحة عند وجود بقية: أوامر الأداة تصل الموديل
                كنص، والموديل مضبوط على العراقي فيمتثل لها أفضل من علم منطقي.
    الفرق بين «ماكو طلبات لسارة» و«لگيت 10 من أصل 340 — تريد أضيّق أكثر؟».

    نفس المعايير خلال 60 ثانية بنفس الجلسة تُجاب من الكاش
    (sessions.cached_query) — الموظف يعيد صياغة سؤاله فلا نعيد الاستعلام.

    `api_key` تُربط بالدالة عبر functools.partial وقت التسجيل (_build_tools)،
    فما تمر بـ args التي يرسلها النموذج."""
    try:
        query = _parse_query_args(args)
    except ValidationError as exc:
        return _invalid_args_error(exc)

    cache_key = sessions.query_cache_key(query)
    if session_id:
        cached = sessions.cached_query(session_id, cache_key)
        if cached is not None:
            return cached

    page = await order_status_provider.search(query, api_key)
    if not page.orders:
        result = {"found": False, "total": 0, "orders": [], "message": "ماكو طلبات مطابقة للمعايير المطلوبة"}
    else:
        result = {
            "found": True,
            "showing": len(page.orders),
            "total": page.total,
            "orders": page.orders,
        }
        if page.has_more:
            result["hint"] = (
                f"هذي {len(page.orders)} من أصل {page.total}. گول للموظف العدد "
                "الكلي، ولا تدّعي إنها كل النتائج. إذا راد الباقي استدعِ الأداة "
                f"مرة ثانية بـ offset={page.offset + len(page.orders)}."
            )
    if session_id:
        sessions.cache_query(session_id, cache_key, result)
    return result


def _build_tools(api_key: str, session_key: str) -> dict:
    """أدوات ميزة الدعم الثلاث، جاهزة لـrun_with_tools/run_decision_rounds.

    مصدر وحيد: /chat و/chat/stream يستدعيانها، فإضافة أداة تصير بمكان واحد
    ولا ننسى أحد المسارين (خطأ سهل جداً لما كان القاموس مكتوباً حرفياً
    بموضعين).

    api_key وsession_id يُربطان بـpartial وقت التسجيل فما يمران بـargs اللي
    يرسلها الموديل — الموديل ما يشوف المفتاح ولا يقدر يزوّره."""
    return {
        "get_order_status": partial(_get_order_status_tool, api_key=api_key, session_id=session_key),
        "count_orders": partial(_count_orders_tool, api_key=api_key, session_id=session_key),
        "get_order_history": partial(_get_order_history_tool, api_key=api_key, session_id=session_key),
    }


async def _count_orders_tool(args: dict, api_key: str, session_id: str = "") -> dict:
    """عدّ الطلبات بمعايير — «كم طلب عدنا هالشهر؟»، «كم طلب قيد التوصيل؟».

    **ليش أداة منفصلة مو عدّ نتيجة get_order_status؟** لأن get_order_status
    يرجّع صفحة وحدة، فعدّها مو العدد الكلي. الموديل ما يعرف الفرق، فلو تركناه
    يعدّ بنفسه راح يعطي رقماً واثقاً وغلط — ونفس علّة الهلوسة القديمة: مأمور
    «لا تجاوب من عندك» بينما ماكو أداة تجاوب، فيخترع.

    بلا أي معيار = العدد الكلي (`{}` أو `{"all": true}`). نفس معايير
    get_order_status (OrderQuery) حتى يكون العدّ والبحث بلغة وحدة للموديل؛
    القرار "عدّ بالخادم أم محلياً" داخل order_gateway.count_for_query."""
    try:
        query = _parse_query_args(args)
    except ValidationError as exc:
        return _invalid_args_error(exc)
    return {"count": await order_status_provider.count_for_query(query, api_key)}


async def _get_order_history_tool(args: dict, api_key: str, session_id: str = "") -> dict:
    """سجل مراحل طلب — «بأي مرحلة الطلب؟»، «ليش متأخر؟»، «شگد صار بالمخزن؟».

    كل حدث يحمل المرحلة، ومتى دخلها الطلب، وكم ساعة بقى بيها
    (`duration_hours`). `duration_hours = None` بآخر حدث معناها **لسا بهذي
    المرحلة** — الموديل مأمور بالبرومبت لا يقرأها كصفر.

    تحتاج `order_id` صراحةً: سجل المراحل لطلب واحد بعينه، ماكو معنى لسجل
    مجموعة طلبات."""
    order_id = args.get("order_id")
    if not order_id:
        return {"error": "لازم تزودني برقم الطلب حتى أجيب مراحله"}
    history = await order_status_provider.get_history(str(order_id), api_key)
    return history or {"error": "ماكو سجل مراحل لهذا الطلب"}


def _format_order_reply(order: dict, mention_order_id: bool = False) -> str:
    """يبني رداً عراقياً حتمياً من بيانات الطلب الحقيقية — بدون موديل.

    يذكر **المنتجات** لا رقم الطلب الداخلي افتراضياً (نفس مبدأ voice_followup
    — الزبون/الموظف يتعرف على طلبه بالمنتج، مو برقم ORD-#### الداخلي)، إلا
    إذا الموظف نفسه سأل به صراحة بالاسم (`mention_order_id=True`) — نفس
    قاعدة SUPPORT_SYSTEM_PROMPT بـapp/features/support/prompts.py ("اذكر
    order_id بس لو الموظف نفسه سأل عنه صراحة بالاسم"). المتصل الوحيد اللي
    يمرّرها True هو فرع البحث بمعرّف الطلب بـ_deterministic_status_answer —
    بحث بالهاتف يبقى بلا رقم طلب لأن الموظف ما ذكره."""
    items = "، ".join(
        f"{it['product_name']} ×{it.get('quantity', 1)}" for it in order.get("items", [])
    )
    subject = items or "طلبك"
    reply = f"هلا بيك، {subject} حالته: {order['status']}"
    if mention_order_id:
        reply += f" (طلب {order['order_id']})"
    if order.get("eta"):
        reply += f"، والوصول المتوقع {order['eta']}"
    # current_stage/assigned_transporter (TODO بـsystem_backend_schema.py):
    # None حالياً لحد ما باك اند السستم يربطهم — الشرط يمنع إلحاق "None"
    # نصياً بالرد لو وصلت فاضية، فالسلوك الحالي (بدون هالحقلين) ما يتغيّر.
    if order.get("current_stage"):
        reply += f" (مرحلة: {order['current_stage']})"
    if order.get("assigned_transporter"):
        reply += f"، المندوب المسؤول: {order['assigned_transporter']}"
    return reply + "."


def extract_order_id(message: str) -> Optional[str]:
    """يستخرج معرّف الطلب بصيغته القياسية ORD-#### من نص الزبون الخام.

    يقبل «ord 1001» و«ORD_1001» و«ORD-١٠٠١» — التطبيع يوحّدها كلها."""
    match = _ORDER_ID_RE.search(normalize(message, keep_punctuation=True))
    return f"ORD-{match.group(1)}" if match else None


def extract_phone(message: str) -> Optional[str]:
    """يستخرج رقم هاتف عراقي بصيغته القياسية 07XXXXXXXXX من نص الزبون الخام.

    يقبل الأرقام العربية-الهندية، والفواصل والشرطات داخل الرقم، والمقدمة
    الدولية (+964 / 00964 / 964) اللي تحلّ محل الصفر. يرجع None إذا ما وُجد
    رقم بطول عراقي صحيح (11 خانة تبدأ بـ 07)."""
    normalized = normalize(message, keep_punctuation=True)
    match = _PHONE_RE.search(normalized)
    if not match:
        return None

    digits = _NON_DIGITS_RE.sub("", match.group())
    digits = digits.removeprefix("00").removeprefix("964")
    if not digits.startswith("0"):
        digits = "0" + digits
    return digits if len(digits) == 11 and digits.startswith("07") else None


async def _known_statuses(api_key: str, session_id: str = "") -> List[str]:
    """الحالات الموجودة فعلاً بالبيانات — مشتقة من استعلام حي (أو كاش
    الجلسة، انظر _list_all_cached)، مو مكتوبة بالكود ولا محفوظة محلياً
    عبر الجلسات."""
    orders = await _list_all_cached(session_id, api_key)
    seen = []
    for order in orders:
        status = str(order.get("status", "")).strip()
        if status and status not in seen:
            seen.append(status)
    return seen


# ---------------------------------------------------------------------------
# فهم سؤال الموظف — البديل عن جداول الكلمات المحذوفة
# ---------------------------------------------------------------------------
#
# **ليش انحذفت الجداول.** كان الفهم بمطابقة نصية على قوائم مكتوبة بيد:
# _STATUS_SYNONYMS ("مكتمله"→تسليم، "بالطريق"→توصيل…)، _COUNT_WORDS،
# _LATEST_WORDS، _LIST_ALL_WORDS، _CONTACT_REQUEST_WORDS، _DATE_RANGE_HINTS.
# كل صيغة مو بالقائمة تفشل بصمت وتروح لمسار الموديل العام، وكل لهجة أو لغة
# جديدة تحتاج نسخة كاملة من الجداول.
#
# **والاعتراض القديم على النموذج — وكيف انحلّ.** توثيق هذا الملف كان يقول:
# «تُحسم هنا لا بالموديل لأن الموديل يخترع معرّفات وحالات» (مرصود فعلياً:
# گال ORD-1002 حالته "قيد التوصيل" وهي "تم التسليم"). الاعتراض صحيح تماماً
# **لو** خلّينا النموذج يكتب حالة نصاً حراً. لكن هنا القوائم المسموحة
# **مبنية من البيانات الحية** (_known_statuses/_known_transporters) وتُمرَّر
# كـenum بـguided_json — فالنموذج ما يقدر فيزيائياً يرجّع حالة مو موجودة
# بباك اند السستم، ولا اسم مندوب مخترَع.
#
# وهذا **أقوى** من الجداول المحذوفة: المرادفات كانت تُطابق نصياً فتقبل أي
# صيغة قريبة، بينما الـenum قائمة مغلقة مشتقة من الحقيقة لحظة السؤال.
#
# ما بقي بالكود عمداً: أرقام الطلب والهاتف والتواريخ الصريحة (regex — صيغ
# لا لغة)، وكل بناء الاستعلام والعرض والصفحات.

SUPPORT_INTENTS = (
    "by_status",       # «شنو الطلبات قيد التوصيل؟»
    "latest",          # «اخر طلب» — الأحدث، بحالة أو مطلقاً
    "by_transporter",  # «مندوب فلان عنده كم طلب؟»
    "by_date_range",   # «الشحنات من الشهر الماضي»
    "list_all",        # «كل الطلبات» / «كم طلب عدنا؟»
    "contacts",        # «انطيني ارقام الهواتف»
    "none",            # مو استعلام دفتر — يروح لمسار الموديل+الأدوات
)


@dataclass
class SupportQuery:
    """ما فهمه النموذج من سؤال الموظف — بعد التثبّت بجهة الخادم."""
    intent: str
    status: Optional[str] = None
    transporter: Optional[str] = None
    wants_count: bool = False
    relative_range: str = "none"


def build_support_query_schema(statuses: List[str], transporters: List[str]) -> dict:
    """يبني مخطط guided_json من **البيانات الحية** لحظة السؤال.

    المخطط يُبنى بكل طلب لا مرة وحدة عند الإقلاع: الحالات والمندوبين
    يتغيرون بباك اند السستم، ومخطط مجمَّد يعني أن حالة جديدة تنضاف للنظام
    ما يقدر النموذج يختارها — وهي بالضبط المشكلة اللي تجنّبها التصميم
    الأصلي لما اشتق _known_statuses من البيانات بدل ما يكتبها بالكود."""
    return {
        "type": "object",
        "properties": {
            "intent": {"type": "string", "enum": list(SUPPORT_INTENTS)},
            # "none" قيمة صريحة لا null — تبسّط الفحص وتتجنب اختلاف تعامل
            # المولّدات المقيَّدة مع القيم الفارغة.
            "status": {"type": "string", "enum": list(statuses) + ["none"]},
            "transporter": {"type": "string", "enum": list(transporters) + ["none"]},
            "wants_count": {"type": "boolean"},
            "relative_range": {"type": "string", "enum": list(RELATIVE_RANGES)},
        },
        "required": ["intent", "status", "transporter", "wants_count", "relative_range"],
        "additionalProperties": False,
    }


def parse_support_query(
    raw: Optional[str], statuses: List[str], transporters: List[str],
) -> SupportQuery:
    """يفكّك ويتثبّت من مخرَج النموذج. أي شذوذ يسقط لـintent="none" بأمان،
    فيروح السؤال لمسار الموديل+الأدوات العادي بدل ما يرجّع قائمة غلط.

    نتثبّت رغم أن guided_json يضمن الشكل، لأن الضمانة تجي من خدمة خارجية
    عبر الشبكة: إصدار يتغيّر، إعداد يُنسى، مسار احتياطي يشتغل بلا تقييد."""
    if not raw:
        return SupportQuery("none")
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("مخرَج فهم سؤال الدعم مو JSON صالح: %r", raw[:200])
        return SupportQuery("none")
    if not isinstance(data, dict):
        return SupportQuery("none")

    intent = data.get("intent")
    if intent not in SUPPORT_INTENTS:
        return SupportQuery("none")

    # الحالة واسم المندوب يُقبلان **فقط** لو كانا من البيانات الحية حرفياً.
    status = data.get("status")
    status = status if status in statuses else None
    transporter = data.get("transporter")
    transporter = transporter if transporter in transporters else None

    rel = data.get("relative_range")
    rel = rel if rel in RELATIVE_RANGES else "none"

    return SupportQuery(
        intent=intent,
        status=status,
        transporter=transporter,
        wants_count=bool(data.get("wants_count")),
        relative_range=rel,
    )


async def understand_support_query(
    message: str, api_key: str, history: Optional[List[dict]] = None, session_id: str = "",
) -> SupportQuery:
    """يحوّل سؤال الموظف الحر لاستعلام مُثبَّت عبر النموذج بمخطط مقيَّد.

    `history` تُمرَّر للنموذج لفهم المتابعات: الموظف يسأل «مكتمله» ثم يوضّح
    «المكتمله تعني التي تم توصيلها» ثم يگول «اعطني هذه الطلبات» — الرسالة
    الأخيرة بلا سياق ما تعني شي، ومعه تعني حالة التسليم. (كان هذا يُعالَج
    بـ_FOLLOWUP_WORDS وبحث رجعي بالتاريخ؛ النموذج يسوّيها طبيعياً.)

    بلا نموذج جاهز يرجع intent="none" — الأسئلة اللي فيها رقم طلب أو هاتف
    تبقى تشتغل محلياً لأنها تُحسم بـregex قبل الوصول هنا."""
    statuses = await _known_statuses(api_key, session_id)
    transporters = await _known_transporters(api_key, session_id)

    if not llm_engine.ready:
        return SupportQuery("none")

    messages = build_support_query_prompt(message, history, statuses, transporters)
    raw = await llm_engine.generate_full(
        llm_engine.render_prompt(messages),
        max_tokens=96,
        # استخراج لا صياغة — نريد نفس المخرَج لنفس السؤال بكل مرة، حتى يبقى
        # سلوك الدعم قابلاً لإعادة الإنتاج عند مراجعة أي شكوى.
        temperature=0.0,
        guided_json=build_support_query_schema(statuses, transporters),
    )
    return parse_support_query(raw, statuses, transporters)


# «مندوب فلان عنده كم طلب؟» — عدّ/سرد الطلبات الموكَّلة لمندوب توصيل معيّن.
# نفس فكرة _known_statuses أعلاه بالضبط، بس مقابل حقل
# assigned_transporter بدل status. الحقل TODO بـ
# app/system_backend_schema.py::SystemOrder (باك اند السستم لسا ما يربط
# assigned_transporter_id → transporters.name)، فالدالتين ترجعان قائمة/None
# فاضية لحد الآن — فور ما الحقل يوصل بالبيانات الحقيقية، هذا الجزء يشتغل
# بلا أي تعديل إضافي (نفس مبدأ TODO الموثَّق بأعلى app/order_gateway.py).


async def _known_transporters(api_key: str, session_id: str = "") -> List[str]:
    """اسماء المندوبين الموجودة فعلاً بالبيانات — نفس نمط _known_statuses:
    مشتقة من استعلام حي (أو كاش الجلسة)، مو مكتوبة بالكود."""
    orders = await _list_all_cached(session_id, api_key)
    seen = []
    for order in orders:
        name = str(order.get("assigned_transporter") or "").strip()
        if name and name not in seen:
            seen.append(name)
    return seen


async def extract_transporter(message: str, api_key: str, session_id: str = "") -> Optional[str]:
    """يستخرج اسم المندوب المذكور برسالة الموظف من أسماء المندوبين
    الحقيقية (_known_transporters)، أو None إذا ما انذكر اسم مطابق."""
    normalized = normalize(message)
    for name in await _known_transporters(api_key, session_id):
        if normalize(name) in normalized:
            return name
    return None


def _format_order_line(order: dict) -> str:
    """سطر طلب واحد بقائمة — يشمل رقم هاتف الزبون لأن البوت داخلي والموظف
    يحتاجه حتى يتصل بيه."""
    items = "، ".join(
        f"{it['product_name']} ×{it.get('quantity', 1)}" for it in order.get("items", [])
    )
    line = f"{order['order_id']} — {items}" if items else str(order["order_id"])
    line += f" | الهاتف {order['phone']}"
    if order.get("eta"):
        line += f" | الوصول {order['eta']}"
    # assigned_transporter (TODO بـsystem_backend_schema.py): None لحد ما
    # باك اند السستم يربطه — نفس شرط eta أعلاه، ما يظهر بالسطر لو فاضي.
    if order.get("assigned_transporter"):
        line += f" | المندوب {order['assigned_transporter']}"
    return line


def _format_order_list(orders: List[dict], heading: str, total: Optional[int] = None) -> str:
    """قائمة طلبات مبنية حتمياً من المصدر — كل معرّف وحالة بيها من
    البيانات حرفياً، ما بيها ولا رقم من عند الموديل. `total` (العدد الكلي
    بالنظام) يُعرض بالعنوان لو مُرِّر، وإلا عدد المعروض."""
    if not orders:
        return f"ماكو {heading} حالياً."
    lines = "\n".join("• " + _format_order_line(o) for o in orders)
    return f"{heading} ({total if total is not None else len(orders)}):\n{lines}"


# القوائم الحتمية تُقصّ لعشرين سطراً مع ذكر العدد الكلي صراحةً (العطل B8).
# عشرون رقم عملي: يملأ شاشة واحدة ويبقى مقروءاً. الجملة الختامية ليست
# تجميلاً — بدونها يظن الموظف أن العشرين هي كل شيء، وهو نفس عطل الكتمان (B1)
# الذي نصلحه بجهة الموديل. الحد لازم يُعلَن لأي مستهلك، إنساناً كان أم نموذجاً.
_MAX_LISTED = 20


def _format_page(page: PagedOrders, heading: str) -> str:
    reply = _format_order_list(page.orders, heading, total=page.total)
    if page.has_more:
        reply += f"\n\n(معروض {len(page.orders)} من أصل {page.total} — ضيّق البحث للباقي.)"
    return reply


# متابعة تشير لجواب سابق بلا ما تسمّي الحالة («اعطني هذه الطلبات»، «اي هذول»).
# بلا معالجتها كان الموظف يوضّح قصده فيسقط السؤال للموديل — وهو يرد «أبحثلك
# هسه» بلا ما يبحث شي، لأنه ما عنده وصول للبيانات أصلاً (مرصود بلقطة إنتاج).
async def _bulk_query_answer(
    message: str, api_key: str, query: "SupportQuery", session_id: str = "",
) -> Optional[str]:
    """يجاوب أسئلة الموظف التشغيلية عن دفتر الطلبات — باستعلام حي مباشر (أو
    كاش الجلسة لجلب القائمة الكاملة، انظر _list_all_cached).

    البوت داخلي للشركة، فهذي استعلامات شغل مشروعة مو تسريب بيانات.

    `query` وصلت مُثبَّتة من understand_support_query: النموذج فهم **اللغة**
    وحوّلها لحقول، وكل حقل تحقّق إنه من البيانات الحية. من هنا فصاعداً ما
    اكو نموذج — بناء الاستعلام والعدّ والعرض كلها بالكود."""
    status = query.status

    # «اخر طلب» — الأحدث، بحالة معيّنة أو مطلقاً.
    if query.intent == "latest":
        orders = (
            await order_status_provider.search_by_status(status, api_key)
            if status
            else await _list_all_cached(session_id, api_key)
        )
        if not orders:
            return f"ماكو طلبات {status} حالياً." if status else "ماكو طلبات مسجلة."
        heading = f"آخر طلب {status}" if status else "آخر طلب"
        return f"{heading}:\n• " + _format_order_line(orders[-1])

    # أسئلة الجرد تصير عدّاً حقيقياً (count_for_query — COUNT بالخادم لو دعمه)
    # بدل جلب-ثم-len(): استعلام ينقل آلاف السجلات يتحوّل لرقم واحد (العطل
    # B9). والقوائم تُقصّ بـ_MAX_LISTED مع إعلان العدد الكلي (العطل B8).
    if query.intent == "by_status" and status:
        if query.wants_count:
            total = await order_status_provider.count_for_query(OrderQuery(status=status), api_key)
            return f"عدد الطلبات بحالة «{status}»: {total}."
        page = await order_status_provider.search(
            OrderQuery(status=status, limit=_MAX_LISTED), api_key
        )
        return _format_page(page, f"الطلبات {status}")

    # «مندوب فلان عنده كم طلب؟» — ماكو فلتر مندوب بـOrderQuery (باك اند
    # السستم ما يبحث بيه)، فالفلترة محلية على الدفتر المخزَّن — لكن العرض
    # مسقَّف بنفس _MAX_LISTED.
    if query.intent == "by_transporter" and query.transporter:
        orders = [
            o for o in await _list_all_cached(session_id, api_key)
            if o.get("assigned_transporter") == query.transporter
        ]
        if query.wants_count:
            return f"عدد طلبات المندوب {query.transporter}: {len(orders)}."
        page = PagedOrders(orders=orders[:_MAX_LISTED], total=len(orders))
        return _format_page(page, f"طلبات المندوب {query.transporter}")

    # عدّ/سرد الشحنات ضمن فترة تاريخ — انظر extract_date_range أعلاه. إذا
    # الرسالة فيها رقم هاتف كمان، نضيّق بالرقم فوق التاريخ حتى ما نرجّع
    # شحنات زبائن ثانيين لموظف يسأل عن رقم معيّن بفترة معيّنة.
    if query.intent == "by_date_range":
        date_range = extract_date_range(message, query.relative_range)
        if date_range:
            date_from, date_to = date_range
            oq = OrderQuery(
                date_from=date_from, date_to=date_to,
                phone=extract_phone(message), limit=_MAX_LISTED,
            )
            if query.wants_count:
                total = await order_status_provider.count_for_query(oq, api_key)
                return f"عدد الشحنات بالفترة {date_from} - {date_to}: {total}."
            page = await order_status_provider.search(oq, api_key)
            return _format_page(page, f"الشحنات من {date_from} لين {date_to}")

    if query.intent == "list_all":
        if query.wants_count:
            total = await order_status_provider.count_for_query(OrderQuery(), api_key)
            return f"عدد الطلبات الكلي: {total}."
        page = await order_status_provider.search(OrderQuery(limit=_MAX_LISTED), api_key)
        return _format_page(page, "كل الطلبات")

    # طلب أرقام هواتف بلا حالة محددة: نعطي الطلبات بأرقامها — القائمة
    # أصلاً تحمل الهاتف بكل سطر.
    if query.intent == "contacts":
        page = await order_status_provider.search(OrderQuery(limit=_MAX_LISTED), api_key)
        return _format_page(page, "أرقام هواتف الطلبات")

    return None


async def _deterministic_status_answer(
    message: str, api_key: str, history: Optional[List[dict]] = None, session_id: str = "",
    tool_calls: Optional[List[dict]] = None,
) -> Optional[str]:
    """توجيه حتمي لطلبات التتبع: إذا الرسالة فيها رقم طلب أو هاتف، نستعلم
    من المصدر مباشرة (استدعاء حي، بلا تخزين محلي) ونبني الرد من البيانات
    الحقيقية — بدون تفويض القرار للموديل. السبب (مرصود بالاختبار الفعلي):
    الموديل الحالي لا يستدعي الأداة بموثوقية ويخترع حالات طلب من خياله
    («قيد التجهيز يوصل خلال يوم» لطلب حالته الحقيقية «قيد التوصيل خلال
    يومين») — hallucination خطير بميزة دعم. يرجع None إذا الرسالة ما فيها
    معرّف، فتذهب لمسار الموديل+الأدوات.

    ⚠️ استثناء عمدي: رقم هاتف مع فترة تاريخ («من الشهر الماضي») يتجاوز الرد
    المباشر أعلاه (اللي يرجع كل طلبات الرقم بلا فلترة) ويروح لـ
    _bulk_query_answer، اللي يحسب الفترة (extract_date_range) ويفلتر بالرقم
    **و**التاريخ معاً. النموذج يميّز الحالتين، والحساب يبقى بالكود.

    `tool_calls` (اختياري): لو تم تمرير قائمة، نلحق فيها سجل الاستعلام
    (نفس شكل tool_calls اللي يبنيها run_with_tools) حتى لو الرد جا من هذا
    المسار الحتمي لا من حلقة أدوات الموديل — بلا هذا، صفحة /test ما تعرض أي
    بطاقة "🔧 استدعى الأداة" لطلبات التتبع المباشرة (الحالة الأشيع فعلياً)،
    فيبدو وكأن ماكو استعلام حصل رغم إنه صار فعلاً — انظر لقطة اختبار حقيقية:
    الموظف يسأل برقم هاتف، الرد يوصل صحيح لكن بلا أي إشارة استعلام بالواجهة."""
    order_id = extract_order_id(message)
    if order_id:
        order = await order_status_provider.get_by_order_id(order_id, api_key)
        if tool_calls is not None:
            tool_calls.append({
                "tool": "get_order_status", "args": {"order_id": order_id},
                "result": order or {"error": "ماكو طلب بهذا الرقم"},
            })
        if order:
            return _format_order_reply(order, mention_order_id=True)
        return "والله ماكو طلب بهذا الرقم عدنا — دقّق الرقم وگلي مرة ثانية."

    # ما بيها رقم طلب: نفهم السؤال مرة وحدة هنا، ونمرّر الفهم لـ
    # _bulk_query_answer بدل ما نستدعي النموذج مرتين. هذا أيضاً يحسم سؤال
    # الهاتف أدناه: «طلبات هذا الرقم من الشهر الماضي» لازم تنفلتر بالتاريخ
    # كمان، و«طلبات هذا الرقم» ترجع كلها — والفرق بينهما لغة، ما regex.
    query = await understand_support_query(message, api_key, history, session_id)

    phone = extract_phone(message)
    if phone and query.intent != "by_date_range":
        orders = await order_status_provider.search_by_phone(phone, api_key)
        if tool_calls is not None:
            tool_calls.append({
                "tool": "get_order_status", "args": {"phone": phone},
                "result": {"orders": orders} if orders else {"error": "ماكو طلبات بهذا الرقم"},
            })
        if orders:
            return "هلا بيك، هذي طلباتك: " + " | ".join(_format_order_reply(o) for o in orders)
        return "والله ماكو طلبات مسجلة بهذا الرقم — تأكد من الرقم وگلي."

    # ما بيها معرّف محدد: يمكن استعلام تشغيلي عن دفتر الطلبات (حالة/جرد).
    # يجي **بعد** المعرّفات عمداً: «حالة ORD-1001» لازم ترجع ذاك الطلب بالذات،
    # مو قائمة كل الطلبات اللي بنفس حالته.
    return await _bulk_query_answer(message, api_key, query, session_id)


def _attempted_lookup_args(message: str) -> Optional[dict]:
    """شنو كان المفروض يُستعلَم عنه (رقم طلب أو هاتف) من الرسالة — يُستخدم
    لبناء سجل tool_calls صناعي لما يفشل الاتصال بباك اند السستم (Exception
    تُرمى من داخل استدعاء المزوّد نفسه، فتقفز فوق سطر tool_calls.append
    الطبيعي بـ_deterministic_status_answer)."""
    order_id = extract_order_id(message)
    if order_id:
        return {"order_id": order_id}
    phone = extract_phone(message)
    if phone:
        return {"phone": phone}
    return None


async def _fallback_support_answer(
    message: str, api_key: str, history: Optional[List[dict]] = None, session_id: str = "",
    tool_calls: Optional[List[dict]] = None,
) -> str:
    """يُستخدم فقط إذا لم يكن الموديل متوفراً (محلياً بدون GPU)."""
    deterministic = await _deterministic_status_answer(
        message, api_key, history, session_id, tool_calls=tool_calls
    )
    if deterministic:
        return "[وضع محلي بدون GPU] " + deterministic
    return "[وضع محلي بدون GPU] عطيني رقم الطلب أو رقم الهاتف حتى اكدر اكَولك وين وصل."


@router.post("/chat", response_model=SupportChatResponse)
async def support_chat(req: SupportChatRequest, api_key: str = Depends(require_support_api_key)):
    # يُضبط مرة وحدة هنا ويقرأه order_gateway._headers بكل نداء لباك اند السستم
    # خلال هذا الطلب — بلا تمرير معامل إضافي عبر سلسلة الدوال.
    caller_auth_token.set(req.auth_token)
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)

    # طلبات التتبع (رقم طلب/هاتف بالرسالة) تُجاب حتمياً من المصدر مباشرة —
    # الموديل غير موثوق باستدعاء الأدوات ويخترع حالات طلب (انظر
    # _deterministic_status_answer). الموديل+الأداة فقط للأسئلة العامة.
    #
    # هذا المسار يستدعي order_status_provider مباشرة (خارج run_with_tools،
    # اللي يلتقط استثناءات الأدوات بنفسه) — فباك اند السستم غير المتاح هنا
    # لازم يُلتقط صراحةً بدل ما يطلع 500 عارية للعميل.
    tool_calls: List[dict] = []
    try:
        deterministic = await _deterministic_status_answer(
            req.message, api_key, history, key, tool_calls=tool_calls
        )
    except SystemBackendUnavailable:
        deterministic = "معذرة، تعذّر الوصول لبيانات الطلبات حالياً — جرّب بعد شوي."
        lookup_args = _attempted_lookup_args(req.message)
        if lookup_args:
            tool_calls.append({
                "tool": "get_order_status", "args": lookup_args,
                "result": {"error": "تعذّر الاتصال بباك اند السستم"},
            })
    if deterministic is not None:
        answer = deterministic
        engine_name = "deterministic"
    elif llm_engine.ready:
        # راوتر نية محافظ (app/intent_router.py) — وصلنا هذا الفرع أصلاً لأن
        # الرسالة ما فيها معرّف طلب/هاتف/حالة صريحة (_deterministic_status_
        # answer رجّع None). لو كانت كمان تحية/شكر/هوية بحتة، نتجاوز
        # get_order_status كلياً بدل التعرّض لاستدعاء غير لازم (next.md §2).
        #
        # ما فيه حقن لدفتر الطلبات (docs/fix-plan.md § المرحلة 4): الأدوات
        # (_build_tools) هي مصدر البيانات الوحيد للموديل — يترجم السؤال
        # لمعايير ويستدعي get_order_status/count_orders.
        messages = build_support_prompt(history, req.message)
        if is_pure_chitchat(req.message):
            answer = await answer_without_tools(messages)
            tool_calls = []
        else:
            tools = _build_tools(api_key, key)
            data = await run_with_tools(messages, tools=tools)
            answer = data["final_answer"]
            tool_calls = data.get("tool_calls") or []
        engine_name = "vllm"
    else:
        answer = await _fallback_support_answer(req.message, api_key, history, key, tool_calls=tool_calls)
        engine_name = "fallback"

    sessions.append(key, "user", req.message)
    sessions.append(key, "assistant", answer)

    return SupportChatResponse(
        session_id=session_id, answer=answer, engine=engine_name, tool_calls=tool_calls,
    )


@router.post("/chat/stream")
async def support_chat_stream(req: SupportChatRequest, api_key: str = Depends(require_support_api_key)):
    """نفس منطق /chat، لكن مسار الموديل+الأدوات (الأسئلة العامة فقط — انظر
    _deterministic_status_answer) يُبث توكن-بتوكن فعلياً بنفس أسلوب
    /sales/chat/stream: جولة قرار مصغّرة (get_order_status) ثم جولة نص حرة
    مبثوثة. المسار الحتمي (رقم طلب/هاتف/حالة) يبقى فورياً كما هو — يُرسَل
    كدلتا واحدة لأنه أصلاً بلا زمن استدلال ينتظره العميل."""
    caller_auth_token.set(req.auth_token)
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)

    async def event_source():
        # يُعاد ضبطه داخل المولّد كمان: Starlette يستهلك body_iterator بمهمة
        # قد تكون بسياق منسوخ، فالضبط هنا يضمن بقاء التوكن مرئياً لكل
        # استدعاء لباك اند السستم أثناء البث مهما كانت طريقة الجدولة.
        caller_auth_token.set(req.auth_token)
        tool_calls: List[dict] = []
        try:
            deterministic = await _deterministic_status_answer(
                req.message, api_key, history, key, tool_calls=tool_calls
            )
        except SystemBackendUnavailable:
            deterministic = "معذرة، تعذّر الوصول لبيانات الطلبات حالياً — جرّب بعد شوي."
            lookup_args = _attempted_lookup_args(req.message)
            if lookup_args:
                tool_calls.append({
                    "tool": "get_order_status", "args": lookup_args,
                    "result": {"error": "تعذّر الاتصال بباك اند السستم"},
                })

        if deterministic is not None:
            answer = deterministic
            yield f"data: {json.dumps({'delta': answer}, ensure_ascii=False)}\n\n"
        elif llm_engine.ready:
            # بلا حقن لدفتر الطلبات — نفس مسار /chat غير المتدفق (support_chat).
            messages = build_support_prompt(history, req.message)
            # نفس الراوتر المحافظ أعلاه (support_chat) — رسالة دردشة/هوية
            # بحتة تتجاوز جولة القرار كلياً (working_messages=messages بلا
            # أي رسائل أداة مضافة)، فباقي الدالة (البث الحر) يشتغل بلا أي
            # تغيير إضافي.
            if is_pure_chitchat(req.message):
                working_messages, tool_calls = messages, []
            else:
                tools = _build_tools(api_key, key)
                working_messages, _decision, tool_calls = await run_decision_rounds(messages, tools=tools)

            answer = ""
            async for delta in stream_final_answer(working_messages):
                answer += delta
                yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            if not answer.strip():
                answer = EXHAUSTED_FALLBACK
                yield f"data: {json.dumps({'delta': answer}, ensure_ascii=False)}\n\n"
        else:
            answer = await _fallback_support_answer(req.message, api_key, history, key, tool_calls=tool_calls)
            yield f"data: {json.dumps({'delta': answer}, ensure_ascii=False)}\n\n"

        sessions.append(key, "user", req.message)
        sessions.append(key, "assistant", answer)

        yield "data: " + json.dumps(
            {"done": True, "session_id": session_id, "tool_calls": tool_calls},
            ensure_ascii=False,
        ) + "\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream")
