# -*- coding: utf-8 -*-
"""دعم العملاء: POST /support/chat — تتبع حالة الطلب برقم الطلب أو الهاتف."""

import re
import uuid
from functools import partial
from typing import List, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app import sessions
from app.auth import require_support_api_key
from app.engine import llm_engine
from app.features.support.prompts import build_support_prompt
from app.order_gateway import order_status_provider
from app.system_backend import SystemBackendUnavailable
from app.text_norm import normalize
from app.tool_loop import run_with_tools

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
    النموذج — نفس مبدأ search_products_tool بالمبيعات."""
    order_id = args.get("order_id")
    phone = args.get("phone")
    status = args.get("status")
    if order_id:
        order = await order_status_provider.get_by_order_id(str(order_id), api_key)
        return order or {"error": "ماكو طلب بهذا الرقم"}
    if phone:
        orders = await order_status_provider.search_by_phone(str(phone), api_key)
        return {"orders": orders} if orders else {"error": "ماكو طلبات بهذا الرقم"}
    if status:
        orders = await order_status_provider.search_by_status(str(status), api_key)
        return {"orders": orders} if orders else {"error": f"ماكو طلبات بحالة {status}"}
    if args.get("all"):
        return {"orders": await _list_all_cached(session_id, api_key)}
    return {"error": "لازم تزودني برقم الطلب أو رقم الهاتف أو الحالة"}


def _format_order_reply(order: dict) -> str:
    """يبني رداً عراقياً حتمياً من بيانات الطلب الحقيقية — بدون موديل."""
    items = "، ".join(
        f"{it['product_name']} ×{it.get('quantity', 1)}" for it in order.get("items", [])
    )
    reply = f"هلا بيك، طلبك {order['order_id']} حالته: {order['status']}"
    if items:
        reply += f" ({items})"
    if order.get("eta"):
        reply += f"، والوصول المتوقع {order['eta']}"
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
    message: str, api_key: str, history: Optional[List[dict]] = None, session_id: str = ""
) -> Optional[str]:
    """توجيه حتمي لطلبات التتبع: إذا الرسالة فيها رقم طلب أو هاتف، نستعلم
    من المصدر مباشرة (استدعاء حي، بلا تخزين محلي) ونبني الرد من البيانات
    الحقيقية — بدون تفويض القرار للموديل. السبب (مرصود بالاختبار الفعلي):
    الموديل الحالي لا يستدعي الأداة بموثوقية ويخترع حالات طلب من خياله
    («قيد التجهيز يوصل خلال يوم» لطلب حالته الحقيقية «قيد التوصيل خلال
    يومين») — hallucination خطير بميزة دعم. يرجع None إذا الرسالة ما فيها
    معرّف، فتذهب لمسار الموديل+الأدوات."""
    order_id = extract_order_id(message)
    if order_id:
        order = await order_status_provider.get_by_order_id(order_id, api_key)
        if order:
            return _format_order_reply(order)
        return "والله ماكو طلب بهذا الرقم عدنا — دقّق الرقم وگلي مرة ثانية."

    phone = extract_phone(message)
    if phone:
        orders = await order_status_provider.search_by_phone(phone, api_key)
        if orders:
            return "هلا بيك، هذي طلباتك: " + " | ".join(_format_order_reply(o) for o in orders)
        return "والله ماكو طلبات مسجلة بهذا الرقم — تأكد من الرقم وگلي."

    # ما بيها معرّف محدد: يمكن استعلام تشغيلي عن دفتر الطلبات (حالة/جرد).
    # يجي **بعد** المعرّفات عمداً: «حالة ORD-1001» لازم ترجع ذاك الطلب بالذات،
    # مو قائمة كل الطلبات اللي بنفس حالته.
    return await _bulk_query_answer(message, api_key, history, session_id)


async def _fallback_support_answer(
    message: str, api_key: str, history: Optional[List[dict]] = None, session_id: str = ""
) -> str:
    """يُستخدم فقط إذا لم يكن الموديل متوفراً (محلياً بدون GPU)."""
    deterministic = await _deterministic_status_answer(message, api_key, history, session_id)
    if deterministic:
        return "[وضع محلي بدون GPU] " + deterministic
    return "[وضع محلي بدون GPU] عطيني رقم الطلب أو رقم الهاتف حتى اكدر اكَولك وين وصل."


@router.post("/chat", response_model=SupportChatResponse)
async def support_chat(req: SupportChatRequest, api_key: str = Depends(require_support_api_key)):
    session_id = req.session_id or str(uuid.uuid4())
    key = _SESSION_PREFIX + session_id
    history = sessions.get(key)
    messages = build_support_prompt(history, req.message)

    # طلبات التتبع (رقم طلب/هاتف بالرسالة) تُجاب حتمياً من المصدر مباشرة —
    # الموديل غير موثوق باستدعاء الأدوات ويخترع حالات طلب (انظر
    # _deterministic_status_answer). الموديل+الأداة فقط للأسئلة العامة.
    #
    # هذا المسار يستدعي order_status_provider مباشرة (خارج run_with_tools،
    # اللي يلتقط استثناءات الأدوات بنفسه) — فباك اند السستم غير المتاح هنا
    # لازم يُلتقط صراحةً بدل ما يطلع 500 عارية للعميل.
    try:
        deterministic = await _deterministic_status_answer(req.message, api_key, history, key)
    except SystemBackendUnavailable:
        deterministic = "معذرة، تعذّر الوصول لبيانات الطلبات حالياً — جرّب بعد شوي."
    if deterministic is not None:
        answer = deterministic
        engine_name = "deterministic"
    elif llm_engine.ready:
        tools = {"get_order_status": partial(_get_order_status_tool, api_key=api_key, session_id=key)}
        data = await run_with_tools(messages, tools=tools)
        answer = data["final_answer"]
        engine_name = "vllm"
    else:
        answer = await _fallback_support_answer(req.message, api_key, history, key)
        engine_name = "fallback"

    sessions.append(key, "user", req.message)
    sessions.append(key, "assistant", answer)

    return SupportChatResponse(session_id=session_id, answer=answer, engine=engine_name)
