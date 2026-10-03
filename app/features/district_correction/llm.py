"""Optional OpenAI-compatible batch resolver, always validated by the caller."""

import json
import re

import httpx

from app.config import Settings
from .models import CaseRequest

SYSTEM_PROMPT = """You match Iraqi delivery addresses to a courier company's official district list
(allowedDistricts, taken from the company's Excel sheet for this governorate). Understand each case by
meaning, like a local dispatcher would; do not just compare letters.

How to read a case:
- The district field is free text typed by people. It may contain the district plus streets, landmarks,
  house numbers, the governorate name, or words such as حي / منطقة / مجمع / محلة / قضاء / ناحية / جمعية.
- Spelling varies: ة/ه, ى/ي, أ/إ/ا, with or without ال, missing or extra letters,
  Eastern or Western digits, and typos. Read addresses in Arabic and preserve catalog names literally.
- The governorate itself (for example بغداد, البصرة, واسط) is context, not a district, unless the list
  contains a district that the text clearly names.
- suggestedDistrict, when present, comes from spelling similarity. Verify it against the meaning of the
  whole text; replace it when another allowed district fits better; ignore it when it is wrong.
- Iraqi addresses are written area first, then the street, building or landmark inside it. When the text
  names an area and then a street or landmark ("الكرادة شارع الرشيد، بناية 10"), the area is the district
  (الكرادة) and the rest goes to addressDetails, even if that street also appears in allowedDistricts.
  Choose a street- or landmark-named district only when the text names no area at all.
- A governorate center written first (الناصرية، الديوانية، الحلة، الموصل، كربلاء، النجف، العمارة،
  الكوت، السماوة، الرمادي، بعقوبة، بغداد) is context like the governorate. When the text then names
  another allowed district, a town, qadha or neighborhood ("الناصرية الشطرة", "الحلة المسيب"), choose
  that district: it can be tens of kilometres from the center.
- When the list has a combined name for the area and its neighborhood ("الفلوجة - حي الشهداء"), choose
  that combined name over the area alone if the text names that neighborhood.
- Numbers are part of names: شارع 20 is never شارع 40.

What to return for each case:
- correctDistrict: copied exactly, character for character, from allowedDistricts. Never invent or edit a
  name. Keep excelSequence, originalDistrict and stateCode exactly as given.
- addressDetails: everything else that locates the address (streets, landmarks, neighborhoods that are not
  the chosen district) plus the original address, without repeating the district or the governorate.
- status: SPLIT_ADDRESS when other location text was moved to addressDetails, otherwise AI_MATCH.
- reason: one short sentence explaining the choice.
- Choose the allowed district supported by the text, even when the spelling is poor, words are
  missing, or only part of the name is written; judge by meaning, sound and the landmarks mentioned.
  When two different districts fit equally and the address provides no distinguishing evidence, use
  UNRESOLVED. Never guess a district just because it appears in the list.
- Use UNRESOLVED when the text names no related place (for example only "قرب الجامع" or a phone
  number), or the choice is ambiguous; keep correctDistrict equal to originalDistrict and keep the address.

Return JSON only: {"cases": [{excelSequence, originalDistrict, correctDistrict, addressDetails,
stateCode, status, reason}, ...]} with one object per case."""

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_THINKING = re.compile(r"<think>.*?</think>", re.S | re.I)


class LLMError(Exception):
    pass


def response_format(allowed_names: list[str], cases: list[CaseRequest]) -> dict:
    """JSON schema (OpenAI, vLLM and LM Studio all accept it) limiting names to real choices."""
    names = sorted(set(allowed_names) | {case.district for case in cases})
    item = {
        "type": "object",
        "properties": {
            "excelSequence": {"type": "integer", "enum": [case.excelSequence for case in cases]},
            "originalDistrict": {"type": "string"},
            "correctDistrict": {"type": "string", "enum": names},
            "addressDetails": {"type": "string"},
            "stateCode": {"type": "string"},
            "status": {"type": "string", "enum": ["AI_MATCH", "SPLIT_ADDRESS", "UNRESOLVED"]},
            "reason": {"type": "string"},
        },
        "required": ["excelSequence", "originalDistrict", "correctDistrict", "addressDetails",
                     "stateCode", "status", "reason"],
    }
    return {"type": "json_schema", "json_schema": {
        "name": "district_cases",
        "schema": {"type": "object", "properties": {"cases": {
            "type": "array", "items": item, "minItems": len(cases), "maxItems": len(cases)}},
                   "required": ["cases"]},
    }}


def parse_cases(content) -> list:
    """Case list from a model reply, tolerating fences, thinking blocks and surrounding prose."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty content")
    text = _THINKING.sub("", content).strip()
    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(text[start:end + 1])
    if isinstance(parsed, dict) and isinstance(parsed.get("cases"), list):
        return parsed["cases"]
    if isinstance(parsed, list):
        return parsed
    raise ValueError("missing cases array")


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool((self.settings.district_llm_base_url or self.settings.vllm_base_url) and
                    (self.settings.district_llm_model or self.settings.model_name))

    async def resolve(self, company: str, state_code: str, cases: list[CaseRequest],
                      allowed_names: list[str], hints: dict[int, str] | None = None) -> list[dict]:
        hints = hints or {}
        payload = {
            "companyName": company, "stateCode": state_code,
            "allowedDistricts": allowed_names,
            "cases": [{"excelSequence": case.excelSequence, "originalDistrict": case.district,
                       "district": case.district, "address": case.address,
                       "stateName": case.stateName, "stateCode": state_code,
                       "suggestedDistrict": hints.get(case.excelSequence)}
                      for case in cases],
        }
        url = (self.settings.district_llm_base_url or self.settings.vllm_base_url).rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions" if url.endswith("/v1") else "/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.district_llm_api_key}"} if self.settings.district_llm_api_key else {}
        body = {
            "model": self.settings.district_llm_model or self.settings.model_name,
            # Deterministic and without thinking: the schema-constrained answer
            # must fit the timeout, and sampling only adds wrong district picks.
            "temperature": 0,
            "reasoning_effort": "none",
            "chat_template_kwargs": {"enable_thinking": False},
            # Reasons are intentionally short; a compact output cap reduces
            # cloud generation time while leaving room for every structured row.
            "max_tokens": min(8192, max(2048, 700 + len(cases) * 120)),
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "response_format": response_format(allowed_names, cases),
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.district_llm_timeout_seconds) as client:
                response = await client.post(url, json=body, headers=headers)
                if response.status_code in (400, 422):
                    # Servers without structured-output support: ask again for plain JSON text.
                    plain = {key: value for key, value in body.items() if key != "response_format"}
                    response = await client.post(url, json=plain, headers=headers)
                response.raise_for_status()
                return parse_cases(response.json()["choices"][0]["message"]["content"])
        except httpx.TimeoutException as exc:
            raise LLMError("LLM_TIMEOUT") from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("LLM_INVALID_RESPONSE") from exc
