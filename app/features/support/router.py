# -*- coding: utf-8 -*-
"""دعم العملاء: POST /support/chat — تتبع حالة الطلب برقم الطلب أو الهاتف."""

import json
import re
import uuid
from datetime import date, timedelta
from functools import partial
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import sessions
from app.auth import require_support_api_key
from app.config import settings
from app.context_blocks import cap_for_model
from app.engine import llm_engine
from app.features.support.prompts import build_support_prompt
from app.intent_router import is_pure_chitchat
from app.order_gateway import created_at_in_range, order_status_provider
from app.system_backend import SystemBackendUnavailable
from app.text_norm import normalize
from app.tool_loop import (
    EXHAUSTED_FALLBACK,
    answer_without_tools,
    run_decision_rounds,
    run_with_tools,
    stream_final_answer,
)

router = APIRouter(prefix="/support", tags=["support"])

_SESSION_PREFIX = "support:"
_ORDER_ID_RE = re.compile(r"ORD[\s\-_]*(\d+)", re.IGNORECASE)

# حالات الطلب المعروفة بالنظام، وكل صيغة يكتبها بيها الموظف. البوت **داخلي
# للموظفين**، فسؤال «شنو الطلبات قيد التوصيل؟» استعلام تشغيلي مشروع يُجاب من
# المصدر مباشرة — مو طلب بيانات زبون غريب.
#
# ليش حتمي مو للموديل: الموديل مرصود إنه يخترع معرّفات طلبات ويسند لها حالات
# ما تطابق orders.json (گال ORD-1002 و ORD-1003 «قيد التوصيل» بينما حالتهما
# الحقيقية «تم التسليم» و«قيد التجهيز»). قائمة طلبات مخترَعة بميزة تتبع تشغيلي
# أسوأ من لا جواب: الموظف يتصرف على أساسها.
# **الحالات تُشتق من orders.json وقت التشغيل، ما مكتوبة هنا.** أضف حالة جديدة
# للبيانات (أو بدّل مزوّد الطلبات بـAPI حقيقي) وتشتغل فوراً بلا تعديل كود —
# نفس المبدأ المطبَّق بدروع المبيعات (انظر app/guards.py).
#
# المكتوب أدناه **مرادفات لغوية فقط**: كيف يسمّي الموظف العراقي الحالةَ بكلامه
# الدارج. هذي تخص اللغة لا البيانات، فتبقى ثابتة مهما تبدّلت الحالات. كل
# مرادف يُربط بحالة حقيقية بالمطابقة النصية، فإذا ما كانت الحالة موجودة
# بالبيانات ينسقط المرادف تلقائياً.
_STATUS_SYNONYMS = {
    # كلمة الموظف         : كلمة تدل على الحالة بنص الحالة نفسها
    "بالطريق": "توصيل",
    "طالعه": "توصيل",
    "مشحونه": "توصيل",
    "الشحن": "توصيل",
    "بالمخزن": "تجهيز",
    "التحضير": "تجهيز",
    "مكتمله": "تسليم",
    "مكتمل": "تسليم",
    "مكتمة": "تسليم",
    "منجزه": "تسليم",
    "منجز": "تسليم",
    "مسلمه": "تسليم",
    "واصله": "تسليم",
    "وصلت": "تسليم",
    "منتهيه": "تسليم",
    "تم توصيلها": "تسليم",
    "توصلت": "تسليم",
    "الغيت": "ملغي",
    "مرفوضه": "ملغي",
    "مرتجعه": "ملغي",
}

# أسئلة الجرد العام («كل الطلبات»، «كم طلب عدنا؟») — بلا حالة محددة.
_LIST_ALL_WORDS = (
    "كل الطلبات", "جميع الطلبات", "كافه الطلبات", "قائمه الطلبات",
    "لائحه الطلبات", "كم طلب", "عدد الطلبات", "الطلبات كلها",
)

# طلب صريح لبيانات الاتصال — مشروع هنا: الموظف يحتاج رقم الزبون حتى يتصل بيه.
_CONTACT_REQUEST_WORDS = (
    "ارقام الهواتف", "ارقام الهاتف", "رقم الهاتف", "ارقام هواتف",
    "رقم الموبايل", "ارقام الموبايل", "رقم التلفون", "ارقام التلفون",
    "الهواتف", "هواتف",
)

# إشارة فترة تاريخ («من الشهر الماضي»، «من تاريخ ... لين ...») — رسالة فيها
# رقم هاتف **و**إشارة فترة زمنية تتجاوز مسار الرد الحتمي المباشر
# (_deterministic_status_answer يرجع كل طلبات الرقم بلا فلترة تاريخ) وتروح
# لـ_bulk_query_answer، اللي يحسب الفترة فعلياً عبر extract_date_range
# ويطبّقها على الرقم إذا مذكور (انظر أدناه) — رد حتمي، بلا حاجة للموديل.
_DATE_RANGE_HINTS = (
    "من تاريخ", "من يوم", "لين تاريخ", "لغاية", "الى تاريخ", "إلى تاريخ",
    "الشهر الماضي", "الاسبوع الماضي", "الأسبوع الماضي", "الاسبوع اللي طاف",
    "من الشهر", "من الاسبوع", "بين تاريخ", "خلال الفترة", "بفترة",
)


def _mentions_date_range(message: str) -> bool:
    return any(h in message for h in _DATE_RANGE_HINTS)


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


def _resolve_relative_range(message: str, today: Optional[date] = None) -> Optional[Tuple[str, str]]:
    """يحسب (date_from, date_to) من عبارة نسبية دارجة («الشهر الماضي»،
    «الأسبوع الماضي»، «اليوم»، «امس»)، أو None إذا ما لگى وحدة معروفة.

    `today` قابلة للتمرير للاختبار (تاريخ ثابت بدل تاريخ التشغيل الفعلي)."""
    today = today or date.today()
    normalized = normalize(message)  # توحّد الهمزات: الأسبوع/امس تصير الاسبوع/امس

    if "الشهر الماضي" in normalized or "الشهر اللي طاف" in normalized:
        first_of_this_month = today.replace(day=1)
        last_of_prev_month = first_of_this_month - timedelta(days=1)
        first_of_prev_month = last_of_prev_month.replace(day=1)
        return first_of_prev_month.isoformat(), last_of_prev_month.isoformat()

    if "الاسبوع الماضي" in normalized or "الاسبوع اللي طاف" in normalized:
        # آخر ٧ أيام قبل اليوم — تعريف عملي بسيط، لا تقويم أسبوعي رسمي (السبت-الجمعة مثلاً).
        end = today - timedelta(days=1)
        start = end - timedelta(days=6)
        return start.isoformat(), end.isoformat()

    # عمداً بلا "اليوم"/"امس" كوحدهما: كلمات عامة تنورد بأي حديث عادي
    # («شلونك اليوم؟») بلا أي علاقة بفترة شحنات — إشارة ضعيفة جداً لوحدها
    # (بعكس «الشهر الماضي»/«الاسبوع الماضي» أعلاه، عبارات مركّبة نادرة
    # بالدردشة العادية). لو الموظف يريد تاريخ اليوم/أمس بالضبط، يذكره
    # صراحة كتاريخ (ISO أو يوم/شهر/سنة) وتلتقطه _extract_explicit_dates.
    return None


def extract_date_range(message: str) -> Optional[Tuple[str, str]]:
    """يحسب فترة (date_from, date_to) بصيغة ISO من رسالة الموظف الخام، أو
    None إذا ما لگى فترة واضحة — يغذّي فلترة _bulk_query_answer المحلية عبر
    created_at_in_range (app/order_gateway.py).

    الترتيب: (١) تاريخان صريحان بالرسالة → أصغرهما date_from وأكبرهما
    date_to بغض النظر عن ترتيب ذكرهما. (٢) تاريخ صريح واحد بس → يُعتبر
    يوماً واحداً. (٣) عبارة نسبية دارجة (_resolve_relative_range)."""
    explicit = _extract_explicit_dates(message)
    if len(explicit) >= 2:
        ordered = sorted(explicit)
        return ordered[0], ordered[-1]
    if len(explicit) == 1:
        return explicit[0], explicit[0]
    return _resolve_relative_range(message)

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
    """list_all() مع كاش بحدود الجلسة — انظر sessions.cached_orders().

    دفتر الطلبات ما يتغيّر بين رسالتين متتاليتين من نفس الموظف، وextract_status
    (عبر _known_statuses) يستدعي list_all مرتين أو أكثر بكل رسالة واحدة. أول
    استدعاء بالجلسة يجيب من باك اند السستم ويخزّن؛ الباقي يقرأ من الذاكرة."""
    cached = sessions.cached_orders(session_id)
    if cached is not None:
        return cached
    orders = await order_status_provider.list_all(api_key)
    sessions.cache_orders(session_id, orders)
    return orders


async def _get_order_status_tool(args: dict, api_key: str, session_id: str = "") -> dict:
    """أداة الموديل. تدعم أربعة استعلامات — البوت داخلي فكل البيانات متاحة.

    الاستعلام بالحالة والجرد كانا ناقصين، وهذا هو السبب الجذري لهلوسة الموديل:
    كان مأموراً «لا تجاوب من عندك» بينما الأداة ترفض كل صيغة يسألها بالحالة،
    فما بقي أمامه إلا الاختراع.

    `api_key` تُربط بالدالة عبر functools.partial وقت التسجيل بـ
    run_with_tools (انظر support_chat أدناه)، فما تمر بـ args التي يرسلها
    النموذج — نفس مبدأ search_products_tool بالمبيعات.

    رقم الهاتف هو معيار البحث الأساسي (يُفحص قبل order_id): الزبون يتذكره
    دايماً بعكس رقم الطلب الداخلي، ويدعم فلترة اختيارية بفترة تاريخ
    (date_from/date_to، ISO "YYYY-MM-DD") يحسبها الموديل من كلام الموظف
    الدارج — انظر SUPPORT_SYSTEM_PROMPT."""
    order_id = args.get("order_id")
    phone = args.get("phone")
    status = args.get("status")
    if phone:
        orders = await order_status_provider.search_by_phone(
            str(phone), api_key,
            date_from=args.get("date_from"), date_to=args.get("date_to"),
        )
        return {"orders": orders} if orders else {"error": "ماكو طلبات بهذا الرقم بالفترة المطلوبة"}
    if order_id:
        order = await order_status_provider.get_by_order_id(str(order_id), api_key)
        return order or {"error": "ماكو طلب بهذا الرقم"}
    if status:
        orders = await order_status_provider.search_by_status(str(status), api_key)
        return {"orders": orders} if orders else {"error": f"ماكو طلبات بحالة {status}"}
    if args.get("all"):
        orders = await _list_all_cached(session_id, api_key)
        orders = cap_for_model(
            orders, settings.max_injected_records,
            label=f"get_order_status(all=true) (session={session_id})",
        )
        return {"orders": orders}
    return {"error": "لازم تزودني برقم الطلب أو رقم الهاتف أو الحالة"}


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


async def extract_status(message: str, api_key: str, session_id: str = "") -> Optional[str]:
    """يستخرج حالة الطلب المقصودة من سؤال الموظف، أو None.

    مصدر الحالات هو استعلام حي (`_known_statuses`) — أي حالة موجودة فعلياً
    بباك اند السستم تُطابَق فوراً. المرادفات اللغوية (`_STATUS_SYNONYMS`)
    تُترجم كلام الموظف الدارج («مكتمله») لكلمة موجودة بنص الحالة («تسليم»)
    ثم تُطابق على الحالات الحقيقية — فلو ما اكو حالة فيها «تسليم» ينسقط
    المرادف وحده.

    نطابق الأطول أولاً: «قيد التوصيل» تحتوي «التوصيل»، ولو طابقنا الأقصر
    أولاً كان صح بالصدفة هنا وغلط بحالات ثانية."""
    normalized = normalize(message)
    statuses = await _known_statuses(api_key, session_id)
    best: Optional[tuple] = None

    # (١) الحالة مذكورة كما هي بالبيانات، أو جزء دالّ منها.
    for status in statuses:
        status_n = normalize(status)
        candidates = [status_n] + [w for w in status_n.split() if len(w) >= 4]
        for cand in candidates:
            if cand in normalized and (best is None or len(cand) > best[0]):
                best = (len(cand), status)

    if best:
        return best[1]

    # (٢) مرادف دارج → كلمة مفتاحية → الحالة الحقيقية اللي تحتويها.
    for synonym, keyword in _STATUS_SYNONYMS.items():
        if normalize(synonym) not in normalized:
            continue
        for status in statuses:
            if keyword in normalize(status):
                return status
    return None


# «مندوب فلان عنده كم طلب؟» — عدّ/سرد الطلبات الموكَّلة لمندوب توصيل معيّن.
# نفس فكرة extract_status/_known_statuses أعلاه بالضبط، بس مقابل حقل
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


def _format_order_list(orders: List[dict], heading: str) -> str:
    """قائمة طلبات مبنية حتمياً من المصدر — كل معرّف وحالة بيها من
    orders.json حرفياً، ما بيها ولا رقم من عند الموديل."""
    if not orders:
        return f"ماكو {heading} حالياً."
    lines = "\n".join("• " + _format_order_line(o) for o in orders)
    return f"{heading} ({len(orders)}):\n{lines}"


# متابعة تشير لجواب سابق بلا ما تسمّي الحالة («اعطني هذه الطلبات»، «اي هذول»).
# بلا معالجتها كان الموظف يوضّح قصده فيسقط السؤال للموديل — وهو يرد «أبحثلك
# هسه» بلا ما يبحث شي، لأنه ما عنده وصول للبيانات أصلاً (مرصود بلقطة إنتاج).
_FOLLOWUP_WORDS = (
    "هذه الطلبات", "هذي الطلبات", "هذول", "هاي الطلبات", "نفسها", "اياها",
    "اللي گلتلك", "الي گلتلك", "نفس الطلبات", "هيه", "اي هذول",
)

# سؤال عدّ («كم طلب مكتمل؟») — الموظف يريد رقماً لا قائمة. نرجع الاثنين:
# الرقم أولاً ثم التفصيل، حتى ما يضطر يعد بنفسه.
_COUNT_WORDS = ("كم", "شكد", "عدد", "چم")

# سؤال عن الأحدث («اخر طلب»). ترتيب orders.json هو ترتيب الإدخال، فالأخير
# أحدثها — نفس ما راح يرجّعه ORDER BY created_at DESC بالنظام الحقيقي.
_LATEST_WORDS = ("اخر طلب", "آخر طلب", "احدث طلب", "أحدث طلب", "اخر الطلبات", "آخر الطلبات")


async def _bulk_query_answer(
    message: str, api_key: str, history: Optional[List[dict]] = None, session_id: str = ""
) -> Optional[str]:
    """يجاوب أسئلة الموظف التشغيلية عن دفتر الطلبات — باستعلام حي مباشر (أو
    كاش الجلسة لجلب القائمة الكاملة، انظر _list_all_cached).

    البوت داخلي للشركة، فهذي استعلامات شغل مشروعة مو تسريب بيانات. تُحسم هنا
    لا بالموديل لأن الموديل يخترع معرّفات وحالات (انظر _STATUS_SYNONYMS).

    `history` تُستعمل لفهم المتابعات: الموظف يسأل «مكتمله» ثم يوضّح «المكتمله
    تعني التي تم توصيلها» ثم يگول «اعطني هذه الطلبات» — الرسالة الأخيرة بلا
    سياق ما تعني شي، ومعه تعني حالة التسليم."""
    normalized = normalize(message)

    status = await extract_status(message, api_key, session_id)

    # ما بالرسالة حالة صريحة؟ إذا كانت متابعة، ندوّر الحالة برسائل الموظف
    # السابقة — الأحدث أولاً.
    if not status and history and any(w in normalized for w in _FOLLOWUP_WORDS):
        for past in reversed([m for m in history if m.get("role") == "user"]):
            status = await extract_status(past.get("content", ""), api_key, session_id)
            if status:
                break

    wants_count = any(w in normalized for w in _COUNT_WORDS)

    # «اخر طلب» — الأحدث، بحالة معيّنة أو مطلقاً.
    if any(w in normalized for w in _LATEST_WORDS):
        orders = (
            await order_status_provider.search_by_status(status, api_key)
            if status
            else await _list_all_cached(session_id, api_key)
        )
        if not orders:
            return f"ماكو طلبات {status} حالياً." if status else "ماكو طلبات مسجلة."
        heading = f"آخر طلب {status}" if status else "آخر طلب"
        return f"{heading}:\n• " + _format_order_line(orders[-1])

    if status:
        orders = await order_status_provider.search_by_status(status, api_key)
        heading = f"الطلبات {status}"
        if wants_count:
            # سؤال عدّ: الرقم أولاً — بس نلحقه بالتفصيل حتى يشوف أي طلبات هي.
            return f"عدد الطلبات {status}: {len(orders)}.\n" + _format_order_list(
                orders, "التفاصيل"
            )
        return _format_order_list(orders, heading)

    # «مندوب فلان عنده كم طلب؟» — يقابل extract_transporter فوق extract_status.
    # None دايماً لحد ما assigned_transporter يوصل من باك اند السستم (TODO)،
    # فالفرع يرجع "ماكو طلبات لهذا المندوب" مؤقتاً — يشتغل فوراً بلا تعديل
    # وقت ما الحقل يتربط فعلياً.
    transporter = await extract_transporter(message, api_key, session_id)
    if transporter:
        orders = [
            o for o in await _list_all_cached(session_id, api_key)
            if o.get("assigned_transporter") == transporter
        ]
        heading = f"طلبات المندوب {transporter}"
        if wants_count:
            return f"عدد طلبات المندوب {transporter}: {len(orders)}.\n" + _format_order_list(
                orders, "التفاصيل"
            )
        return _format_order_list(orders, heading)

    # عدّ/سرد الشحنات ضمن فترة تاريخ (تاريخ وتاريخ) — انظر extract_date_range
    # أعلاه. إذا الرسالة فيها رقم هاتف كمان (مثلاً حالة الاستثناء اللي
    # _deterministic_status_answer يمرّرها هنا بدل الرد المباشر)، نفلتر
    # بالرقم فوق فلترة التاريخ حتى ما نرجّع شحنات زبائن ثانيين لموظف يسأل
    # عن رقم معيّن بفترة معيّنة.
    date_range = extract_date_range(message)
    if date_range:
        date_from, date_to = date_range
        orders = [
            o for o in await _list_all_cached(session_id, api_key)
            if created_at_in_range(o.get("created_at"), date_from, date_to)
        ]
        phone = extract_phone(message)
        if phone:
            orders = [o for o in orders if o.get("phone") == phone]
        heading = f"الشحنات من {date_from} لين {date_to}"
        if wants_count:
            return f"عدد الشحنات بالفترة {date_from} - {date_to}: {len(orders)}.\n" + _format_order_list(
                orders, "التفاصيل"
            )
        return _format_order_list(orders, heading)

    if any(w in normalized for w in _LIST_ALL_WORDS):
        orders = await _list_all_cached(session_id, api_key)
        if wants_count:
            return f"عدد الطلبات الكلي: {len(orders)}.\n" + _format_order_list(
                orders, "التفاصيل"
            )
        return _format_order_list(orders, "كل الطلبات")

    # طلب أرقام هواتف بلا حالة محددة: نعطي كل الطلبات بأرقامها — القائمة
    # أصلاً تحمل الهاتف بكل سطر.
    if any(w in normalized for w in _CONTACT_REQUEST_WORDS):
        orders = await _list_all_cached(session_id, api_key)
        return _format_order_list(orders, "أرقام هواتف الطلبات")

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

    ⚠️ استثناء عمدي: رقم هاتف مع إشارة فترة تاريخ («من الشهر الماضي») يتجاوز
    الرد المباشر أعلاه (اللي يرجع كل طلبات الرقم بلا فلترة) ويروح لـ
    _bulk_query_answer، اللي يحسب الفترة فعلياً (extract_date_range) ويفلتر
    بالرقم **و**التاريخ معاً — حتمي بالكامل، بلا حاجة للموديل هنا. انظر
    _mentions_date_range.

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

    phone = extract_phone(message)
    if phone and not _mentions_date_range(message):
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
    return await _bulk_query_answer(message, api_key, history, session_id)


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
        # دفتر الطلبات الكامل (_list_all_cached — نفس الكاش المستخدَم بالمسار
        # الحتمي أعلاه، انظر cached_orders/cache_orders بـapp/sessions.py)
        # يُحقن بالبرومبت (build_support_prompt) حتى يبحث الموديل باسم الزبون
        # مباشرة بلا استدعاء أداة جديد كل سؤال — انظر next.md.
        orders = cap_for_model(
            await _list_all_cached(key, api_key), settings.max_injected_records,
            label=f"orders injection (session={session_id})",
        )
        messages = build_support_prompt(history, req.message, orders=orders)
        if is_pure_chitchat(req.message):
            answer = await answer_without_tools(messages)
            tool_calls = []
        else:
            tools = {"get_order_status": partial(_get_order_status_tool, api_key=api_key, session_id=key)}
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
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)

    async def event_source():
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
            # نفس دفتر الطلبات المحقون بمسار /chat غير المتدفق (support_chat)
            # — انظر تعليقه هناك.
            orders = cap_for_model(
                await _list_all_cached(key, api_key), settings.max_injected_records,
                label=f"orders injection (session={session_id})",
            )
            messages = build_support_prompt(history, req.message, orders=orders)
            # نفس الراوتر المحافظ أعلاه (support_chat) — رسالة دردشة/هوية
            # بحتة تتجاوز جولة القرار كلياً (working_messages=messages بلا
            # أي رسائل أداة مضافة)، فباقي الدالة (البث الحر) يشتغل بلا أي
            # تغيير إضافي.
            if is_pure_chitchat(req.message):
                working_messages, tool_calls = messages, []
            else:
                tools = {"get_order_status": partial(_get_order_status_tool, api_key=api_key, session_id=key)}
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
