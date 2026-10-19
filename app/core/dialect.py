# -*- coding: utf-8 -*-
"""فهم اللهجة العراقية والرد بيها — طبقة مشتركة لكل الميزات.

مكوّنان يُلحقان برسالة system لكل ميزة:
1. دليل ثابت (أمثلة فهم + أمثلة صياغة صح/غلط) — يعلّم الموديل يقرأ «حبة ماوس»
   كقطعة واحدة ويكتب «راح يوصلك» بدل «وياتك/سيصلك».
2. مرجع RAG ديناميكي — كلمات الزبون نفسها تُبحث بفهرس اللهجة (app/rag)
   ومعانيها تُحقن بالبرومت، فالموديل يشوف معنى كل كلمة عراقية بالرسالة الحالية.

المكالمات الصوتية ما عندها نص زبون قبل المكالمة، فتاخذ الدليل + قاموساً ثابتاً
لمفردات المكالمات (call_dialect_block).
"""

from typing import Any, Iterable, List

from app.config import settings
from app.core.context_blocks import words_context_block
from app.rag import search as search_words
from app.rag.commerce_terms import CALL_CATEGORIES, COMMERCE_TERMS

IRAQI_UNDERSTANDING_EXAMPLES = """فهم اللهجة العراقية (اقرأ كل رسالة كعراقي من أهل البلد، مو كمترجم حرفي):
- «اريد لابتوب لينوفو وحبة ماوس لوجيتك» = لابتوب لينوفو ×1 + ماوس لوجيتك ×1 («حبة» = قطعة، مو جزء من اسم المنتج).
- «دزلي حبتين شاحنة مال ايفون» = أرسل لي شاحنَي آيفون، الكمية 2 («شاحنة» = شاحن، «مال» = خاص بـ).
- «شكد سعره؟» / «بيش؟» = كم سعره؟ — «شكد صار الكلي؟» = كم المجموع؟
- «ثبتلي واحد» / «احجزلي» = سجّل طلب قطعة واحدة. «اي ثبته» / «على بركة الله» = موافقة صريحة.
- «لا بطلت» / «خليه» / «ما اريده» = تراجع أو إلغاء — لا تثبّت شيئاً.
- «مو هسه، عكب باجر» = ليس الآن، بعد غد. «باجر العصر» = غداً بعد الظهر.
- «الكرادة يم ساحة الحرية» / «كبال الجامع» = المنطقة الكرادة، و«يم/كبال» (قرب/مقابل) تفاصيل عنوان.
- «250 الف» = 250,000 دينار. «ورقة» = 100 دولار.
- «ماجاني الطلب» = ما وصلني. «طلع عاطل/مضروب» = معطّل أو تالف.
- «ياهو الاحسن؟» = أيّهما أفضل؟ «اكو غيره؟» = هل يوجد بديل؟
- الإملاء العامي طبيعي: گ=ك، چ=ج، «هسه/هسة»، «اريد/أريد»، أرقام عربية أو إنجليزية — افهمها كلها بنفس المعنى."""

IRAQI_REPLY_EXAMPLES = """صياغة ردك بالعراقي (✓ اكتب هيچ، ✗ لا تكتب هيچ):
- ✓ «راح يوصلك بأقرب وقت» — ✗ «وياتك» / «سيصلك».
- ✓ «تم تثبيت طلبك عيوني» — ✗ «تمت معالجة طلبك بنجاح».
- ✓ «اكو / ماكو» — ✗ «يوجد / لا يوجد».
- ✓ «هسه أشوفلك» — ✗ «سأقوم بالتحقق من ذلك».
- ✓ «شكد تريد حبة؟» — ✗ «كم الكمية المطلوبة؟».
- ✓ «انطيني رقمك» — ✗ «يرجى تزويدي برقم هاتفك».
- ✓ «وين ساكن؟ بأي منطقة؟» — ✗ «ما هو عنوان التوصيل؟».
- ✓ «تحب أحجزلك وحدة؟» — ✗ «هل ترغب بإتمام عملية الشراء؟».
- ✓ «عذراً حبيبي» / «معليش» — ✗ «نعتذر عن الإزعاج».
- ✓ «شوكت يناسبك؟ باجر لو عكب باجر؟» — ✗ «ما هو الوقت المناسب لك؟».
- لا خليجي («وايد»، «زين مرة»، «لا يخالف»)، ولا مصري («إزيك»، «عايز»)، ولا شامي («شو»، «هلق»)."""

IRAQI_DIALECT_GUIDE = IRAQI_UNDERSTANDING_EXAMPLES + "\n\n" + IRAQI_REPLY_EXAMPLES


def _text_of(content: Any) -> str:
    """نص رسالة OpenAI سواء كانت string أو أجزاء content (نص + صور)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "") for part in content
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str)
        )
    return ""


def recent_user_text(messages: Iterable[Any], turns: int = 2) -> str:
    """آخر رسائل الزبون (افتراضياً آخر رسالتين) — نص استعلام RAG اللهجة.
    تقبل dict أو كائن pydantic فيه role/content."""
    texts: List[str] = []
    for message in reversed(list(messages)):
        role = message.get("role") if isinstance(message, dict) else getattr(message, "role", None)
        if role != "user":
            continue
        content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
        text = _text_of(content).strip()
        if text:
            texts.append(text)
        if len(texts) >= turns:
            break
    return "\n".join(reversed(texts))


def dialect_reference(text: str) -> str:
    """مرجع RAG لكلمات النص — فارغ إذا ماكو نص أو ماكو مطابقة."""
    if not text or not text.strip():
        return ""
    # واو العطف الملتصقة («وحبة ماوس») تخفي الكلمة عن الفهرس — نضيف نسخة بلاها
    # بجانب الأصل (الأصل يبقى حتى ما تضيع كلمات تبدأ بالواو مثل «وين»).
    detached = [t[1:] for t in text.split() if t.startswith("و") and len(t) > 3]
    query = " ".join([text, *detached])
    seen, unique = set(), []
    for hit in search_words(query, top_k=settings.rag_top_k + 3):
        key = hit.get("word") or hit.get("text")
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    return words_context_block(unique[: settings.rag_top_k + 1])


def dialect_block(user_text: str = "") -> str:
    """الدليل الثابت + مرجع RAG لكلام الزبون الحالي — يُلحق بآخر رسالة system."""
    return "\n\n" + IRAQI_DIALECT_GUIDE + dialect_reference(user_text)


MSA_REPLY_RULE = """# لغة الرد
- افهم كلام الزبون بأي لهجة (العراقية خاصة) حسب الدليل أعلاه، لكن ردّ دائماً بعربية فصحى مبسّطة وواضحة يفهمها الجميع، بلا كلمات عامية.
- جمل قصيرة مألوفة، مثل: "أهلاً بك"، "هل وصلك الطلب؟"، "شكراً لوقتك"."""


def understanding_block(user_text: str = "") -> str:
    """فهم اللهجة فقط (بلا أسلوب الرد العراقي) + قاعدة الرد بالفصحى المبسطة."""
    return "\n\n" + IRAQI_UNDERSTANDING_EXAMPLES + dialect_reference(user_text) + "\n\n" + MSA_REPLY_RULE


def call_understanding_block(include_reply_rule: bool = True) -> str:
    """للمكالمات الصوتية: فهم اللهجة ومفرداتها، والرد بالفصحى المبسطة."""
    lines = [f"- {word}: {meaning}" for word, meaning, category in COMMERCE_TERMS if category in CALL_CATEGORIES]
    return (
        "\n\n# فهم كلام الزبون\n" + IRAQI_UNDERSTANDING_EXAMPLES
        + "\n\nمفردات قد تسمعها بالمكالمة ومعناها:\n" + "\n".join(lines)
        + ("\n\n" + MSA_REPLY_RULE if include_reply_rule else "")
    )


def call_dialect_block() -> str:
    """للمكالمات الصوتية: الدليل + قاموس ثابت لمفردات التأكيد والمواعيد والشكاوى."""
    lines = [f"- {word}: {meaning}" for word, meaning, category in COMMERCE_TERMS if category in CALL_CATEGORIES]
    return (
        "\n\n# اللهجة العراقية\n" + IRAQI_DIALECT_GUIDE
        + "\n\nمفردات تسمعها بالمكالمة ومعناها:\n" + "\n".join(lines)
    )
