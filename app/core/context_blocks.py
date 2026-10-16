"""Reference blocks used by order intake prompts."""

from typing import List


def words_context_block(rag_words: List[dict]) -> str:
    if not rag_words:
        return ""
    lines = []
    for result in rag_words:
        if result.get("word"):
            lines.append(f"- {result['word']}: {result['meaning']}")
        else:
            lines.append(f"- {result['text']}")
    return "\n\nمعلومات مرجعية عن اللهجة العراقية (استخدمها إذا كانت مفيدة):\n" + "\n".join(lines)


def locations_context_block(rag_locations: List[dict], state_names: List[str]) -> str:
    """Format the official state and district names for order extraction."""
    block = (
        "\n\nقيم city المسموحة حصراً — الأسماء الرسمية للمحافظات بنظام شركة التوصيل"
        " (اكتب الاسم حرفياً كما هو هنا):\n"
        + "، ".join(state_names)
    )
    if rag_locations:
        lines = []
        for result in rag_locations:
            if result["district"]:
                states = "/".join(result["candidates"])
                lines.append(f"- المنطقة «{result['district']}» تتبع محافظة: {states}")
            else:
                lines.append(f"- «{result['state_name']}» محافظة")
        block += (
            "\n\nمرجع جغرافي مؤكد من قاعدة بيانات شركة التوصيل — أسماء وردت بالنص:\n"
            + "\n".join(lines)
            + "\nإذا ذُكرت منطقة من هذا المرجع بالنص فاكتب district باسمها المذكور"
            " أعلاه حرفياً، وcity بمحافظتها المذكورة أعلاه."
        )
    return block
