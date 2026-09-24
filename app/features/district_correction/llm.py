"""Optional OpenAI-compatible batch resolver, always validated by the caller."""

import json

import httpx

from app.config import Settings
from .models import CaseRequest

SYSTEM_PROMPT = """You are an Iraqi district normalization service. For each case choose correctDistrict only
from allowedDistricts. Company and stateCode are hard constraints. Never invent a district. Preserve
excelSequence and originalDistrict. If district text starts with an allowed district, move the remaining
text into addressDetails. Remove only a duplicate district prefix from address. Set status to AI_MATCH
when correctDistrict is chosen from allowedDistricts. If uncertain, set correctDistrict to the original
district, status UNRESOLVED, and preserve the address. Use no other status value. Return JSON only
with cases as an array of objects: excelSequence, originalDistrict, correctDistrict, addressDetails,
stateCode, status."""


class LLMError(Exception):
    pass


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.district_llm_base_url and self.settings.district_llm_model)

    async def resolve(self, company: str, state_code: str, cases: list[CaseRequest],
                      allowed_names: list[str]) -> list[dict]:
        payload = {
            "companyName": company, "stateCode": state_code,
            "allowedDistricts": allowed_names,
            "cases": [{"excelSequence": case.excelSequence, "originalDistrict": case.district,
                       "district": case.district, "address": case.address, "stateCode": state_code}
                      for case in cases],
        }
        url = self.settings.district_llm_base_url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url += "/chat/completions" if url.endswith("/v1") else "/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self.settings.district_llm_api_key}"} if self.settings.district_llm_api_key else {}
        try:
            async with httpx.AsyncClient(timeout=self.settings.district_llm_timeout_seconds) as client:
                response = await client.post(url, json={
                    "model": self.settings.district_llm_model,
                    "temperature": 0,
                    "max_tokens": min(3000, max(512, len(cases) * 120)),
                    "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                 {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                    "response_format": {"type": "json_object"},
                }, headers=headers)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                parsed = json.loads(content)
                if not isinstance(parsed, dict) or not isinstance(parsed.get("cases"), list):
                    raise ValueError("missing cases array")
                return parsed["cases"]
        except httpx.TimeoutException as exc:
            raise LLMError("LLM_TIMEOUT") from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("LLM_INVALID_RESPONSE") from exc
