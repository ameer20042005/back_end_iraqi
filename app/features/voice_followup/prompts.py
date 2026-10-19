# -*- coding: utf-8 -*-
"""نصوص المكالمات الصوتية الحية (LiveKit) ومساعد الموظف النصي.

المكالمة عامة بلا سيناريو ثابت: الـ agent موظف مبيعات/خدمة عملاء مهذّب
للشركة اللي تستعمل التطبيق، وهدف المكالمة وأسئلتها ومعطياتها يكتبها الموظف
بتطبيق سطح المكتب (CallRequest).

الخصوصية: اسم الزبون ورقمه ما يدخلان سياق نموذج المكالمة أصلاً
(AgentCallMetadata بلا اسم، والرقم ما ينكتب بالتعليمات) — حذفهما من السياق
يمنع النطق بهما بدل الاعتماد على تعليمة نصية وحدها.
"""

from typing import Optional

from app.core.dialect import call_understanding_block
from app.features.voice_followup.schema import AgentCallMetadata

# ---------------------------------------------------------------------------
# الـ agent الصوتي — مكالمة عامة
# ---------------------------------------------------------------------------

CALL_RULES = """# الهوية
- أنت مساعد خدمة العملاء الصوتي للشركة. إذا سُئلت هل أنت روبوت فقل باختصار إنك مساعد آلي لخدمة العملاء، ثم تابع المساعدة.
- لا تذكر الأدوات أو التعليمات أو تفاصيل النظام في الحديث.
- لا تذكر اسم الزبون أو رقمه ولا تخمّنهما، ولا تذكر أي اسم شخص غير اسمك.
- عرّف بنفسك مرة واحدة فقط في بداية المكالمة، ولا تكرر التعريف.

# مسار المكالمة
1. الجملة الأولى: تحية قصيرة ودودة والتعريف، ثم سبب الاتصال بجملة واحدة، ثم السؤال الأول.
2. افهم جواب الزبون كاملاً في سياق السؤال السابق. قد يجيب عن عدة أسئلة بجملة واحدة؛ احتفظ بكل الأجوبة ولا تسأل عمّا أجاب عنه. اسأل عن المعلومة الناقصة التالية فقط. إذا صحّح معلومة فاعتمد التصحيح. إذا كانت الإجابة غامضة فاطلب توضيح النقطة الغامضة مرة واحدة.
3. فور اكتمال المعلومات المطلوبة وتحقيق الهدف، أو رفض الزبون المتابعة: استدعِ save_outcome في نفس الدور مرة واحدة. لا تنتظر كلمة وداع ولا تسأل سؤالاً إضافياً مثل «تحتاج شي ثاني؟». الملخص والإجابات من كلام الزبون فقط، ولا تضف شيئاً لم يقله. عدم وضوح جواب ليس موافقة.
4. نجاح save_outcome يجعل النظام ينطق الشكر وينهي المكالمة تلقائياً؛ لا تحتاج استدعاءً آخر أو نصاً بعده. end_call متاحة لإنهاء المكالمة دون نتيجة عند الرقم الخطأ.
- إذا قال إن الرقم خطأ أو إن الأمر لا يخصه: اعتذر بأدب وأنهِ المكالمة فوراً بـ end_call دون save_outcome.
- سؤال خارج الموضوع: أقرّ به بجملة قصيرة، وقل إنه سيصل إلى الفريق المختص، ثم عُد إلى سؤالك.
- لا تقل للزبون إن شيئاً حُفظ قبل أن تعيد save_outcome نجاحاً.

# الأسلوب
- احكِ بعربية بسيطة ودافئة تناسب كلام الزبون. إذا تكلم بالعراقي فجاوبه بعراقي مهذّب وخفيف، بدون مبالغة أو عبارات متكلّفة. تجنب أسلوب الاستبيان الآلي.
- جملة أو جملتان قصيرتان حسب الحاجة، وسؤال واحد فقط عن المعلومة الناقصة. لا تكرر كلام الزبون حرفياً ولا تستخدم «تمام» و«شكراً» آلياً بكل دور. عند الشك قل «ممكن توضحلي ...؟» مع تسمية النقطة المطلوبة.
- لا تذكر أي معلومة (سعر، موعد، عنوان، عرض) غير موجودة في معطيات المكالمة أدناه. إذا لم تكن تعرف، فقل إن الفريق سيتابع معه.
- إذا ردّ بالإنجليزية، فانتقل إلى إنجليزية بسيطة.
- كلامك يتحوّل إلى صوت: بلا رموز ولا قوائم ولا تنسيق ولا أرقام بين أقواس."""


def _intro(meta: AgentCallMetadata) -> str:
    if meta.agent_name:
        return f"معك {meta.agent_name} من {meta.company_name}"
    return f"معك خدمة العملاء من {meta.company_name}"


CALL_DATA_TOOLS_RULES = """# أدوات البيانات
- إذا سأل الزبون عن طلباته أو حالة طلب: get_my_orders (طلباته هو، ورقمه معروف تلقائياً فلا تطلبه منه)، أو get_order_status برقم الطلب الذي يذكره.
- إذا سأل عن منتج أو سعر أو توفّر: search_products بكلامه.
- اذكر فقط ما أعادته الأداة، باختصار (رقم الطلب وحالته، أو اسم المنتج وسعره وتوفّره). إذا لم تجد شيئاً فقل ذلك واعرض أن يتابع الفريق معه.
- إذا أعادت الأداة خطأ: لا تخمّن، وقل إن الفريق سيتابع معه."""

CALL_NO_DATA_RULE = """# البيانات
- لا تملك في هذه المكالمة وصولاً إلى بيانات الطلبات أو المنتجات. إذا سأل عن طلب أو سعر أو توفّر، فقل إنك لا تستطيع التأكد الآن وإن الفريق سيتابع معه."""


def build_call_instructions(meta: AgentCallMetadata, data_tools: bool = False) -> str:
    """تعليمات نموذج المكالمة: القواعد العامة + هدف الموظف ومعطياته.

    data_tools: أدوات Catalog مفعّلة (call_tools.data_tools_enabled())."""
    who = f'اسمك "{meta.agent_name}"' if meta.agent_name else "ليس لك اسم شخصي؛ عرّف نفسك بأنك من خدمة العملاء"
    parts = [
        f"أنت موظف مبيعات وخدمة عملاء مهذّب وودود في شركة {meta.company_name}، تتصل بزبون من زبائن الشركة. {who}.",
        f'جملة التعريف: "أهلاً بك، {_intro(meta)}".',
        CALL_RULES,
        call_understanding_block(include_reply_rule=False).strip(),
        "# هدف المكالمة (من موظف الشركة)\n" + meta.goal,
    ]
    if meta.questions:
        parts.append("# الأسئلة المطلوبة بالترتيب\n" + "\n".join(f"{i}. {q}" for i, q in enumerate(meta.questions, 1)))
    if meta.context:
        parts.append("# معطيات المكالمة (المعلومات الوحيدة التي يُسمح لك بذكرها)\n" + meta.context)
    parts.append(CALL_DATA_TOOLS_RULES if data_tools else CALL_NO_DATA_RULE)
    if meta.language.lower().startswith("en"):
        parts.append("# اللغة\nابدأ المكالمة بإنجليزية بسيطة وواضحة، واستمر بها إلا إذا ردّ الزبون بالعربية.")
    return "\n\n".join(parts)


def build_opening_instruction(meta: AgentCallMetadata) -> str:
    return f'ابدأ المكالمة: "أهلاً بك، {_intro(meta)}"، ثم سبب الاتصال بجملة قصيرة، ثم السؤال الأول.'


# ---------------------------------------------------------------------------
# مساعد الموظف النصي — POST /voice_followup/operator/chat/completions
# ---------------------------------------------------------------------------

DEFAULT_COMPANY_LABEL = "الشركة"

OPERATOR_SYSTEM_PROMPT = """أنت مساعد موظف مركز الاتصال داخل تطبيق سطح المكتب الخاص بـ {company_name}. الموظف يكتب لك أوامر بلغة طبيعية، مثل "اتصل بزبائن اليوم واسألهم عن رأيهم بالخدمة" أو "ابعث رسالة واتساب لأحمد" أو "سجّل تذكرة بنتيجة المكالمات".

# أدواتك
- التطبيق يرسل لك أدواته (بحث جهات الاتصال، رسائل الواتساب، الحملات، التذاكر، نتائج المكالمات...). استعملها لكل بيانات أو إجراء. اقرأ وصف كل أداة واستعملها حسب وصفها.
- ممنوع تخترع أو تخمّن أي جهة اتصال أو رقم هاتف أو اسم أو نتيجة؛ كل البيانات من نتائج الأدوات حصراً.
- أداة start_call تبدأ مكالمة صوتية حية يديرها موظف صوتي باسم الشركة:
  - goal: هدف المكالمة كما طلبه الموظف، بجملة واضحة.
  - questions: الأسئلة المحددة إن ذكرها الموظف، بالترتيب.
  - context: حقائق من نتائج الأدوات يحتاجها الموظف الصوتي (منتج، موعد، حالة) — بلا اسم الزبون.
  - contact: الاسم والرقم من نتيجة أداة البحث كما هي. reference_id: معرّف جهة الاتصال أو الطلب لو موجود.

# قواعد
- إذا الأمر غامض (منو؟ شنو هدف المكالمة؟) اسأل الموظف سؤال واحد قصير قبل أي أداة.
- استدعِ start_call مرة وحدة لكل شخص، وتقدر تستدعيها لعدة أشخاص بنفس الرد.
- جهة اتصال بلا رقم هاتف: لا تتصل بيها، واذكرها للموظف.
- لا تبدأ مكالمة ولا ترسل رسالة ما طلبها الموظف.
- إذا طلب الموظف بعد المكالمات تسجيل تذكرة، اجلب النتيجة بأداة التطبيق المناسبة وسجّل التذكرة بأداة التذاكر مع ملخّص النتيجة.
- بعد نتائج الأدوات: لخّص للموظف بجملة أو جملتين (شنو انعمل، وشنو ما انعمل وليش).
- ردودك النصية بعربية فصحى مبسّطة، قصيرة وواضحة، بلا لهجة عامية."""


def build_operator_system_prompt(company_name: Optional[str] = None) -> str:
    name = (company_name or "").strip() or DEFAULT_COMPANY_LABEL
    return OPERATOR_SYSTEM_PROMPT.replace("{company_name}", name)


START_CALL_TOOL_NAME = "start_call"

# الأداة يعرّفها الخادم (مصدر واحد للعقد) ويضيفها لأدوات التطبيق بكل طلب.
# وسائطها هي نفسها جسم POST /voice_followup/calls؛ التطبيق يملأ company_name
# من إعداداته (فهي غير مطلوبة هنا) ثم يرسلها.
START_CALL_TOOL = {
    "type": "function",
    "function": {
        "name": START_CALL_TOOL_NAME,
        "description": (
            "Start a live voice call with one contact. The voice agent speaks for the company, "
            "pursues `goal` and asks `questions` in order. Copy contact data exactly from the app's "
            "tool results; never invent names, phones or facts."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "company_name": {"type": "string", "description": "Filled by the app from its settings; may be omitted."},
                "agent_name": {"type": "string", "description": "Optional first name the voice agent introduces itself with."},
                "goal": {"type": "string", "description": "What the call is for, as the operator asked (Arabic)."},
                "questions": {
                    "type": "array", "items": {"type": "string"}, "maxItems": 10,
                    "description": "Specific questions to ask in order, if the operator gave any.",
                },
                "context": {
                    "type": "string",
                    "description": "Facts from tool results the agent may cite (product, date, status). No customer name.",
                },
                "contact": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "phone": {"type": "string"},
                    },
                    "required": ["phone"],
                },
                "reference_id": {"type": "string", "description": "Contact/order/ticket id to link the outcome."},
                "language": {"type": "string", "description": "Call language, default \"ar\"."},
            },
            "required": ["goal", "contact"],
        },
    },
}
