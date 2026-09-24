"""Public Spring-facing request and response contracts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Status = Literal["EXACT_MATCH", "NORMALIZED_MATCH", "FUZZY_MATCH", "AI_MATCH", "SPLIT_ADDRESS", "UNRESOLVED"]


class CaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    excelSequence: int
    stateName: str = Field(default="", max_length=100)
    stateCode: str = Field(min_length=1, max_length=20)
    district: str = Field(default="", max_length=300)
    address: str = Field(default="", max_length=1000)


class CorrectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    companyName: str = Field(min_length=1)
    cases: list[CaseRequest] = Field(min_length=1, max_length=10000)

    @model_validator(mode="after")
    def unique_sequences(self):
        numbers = [case.excelSequence for case in self.cases]
        if len(numbers) != len(set(numbers)):
            raise ValueError("excelSequence must be unique within a request")
        return self


class CaseResponse(BaseModel):
    excelSequence: int
    originalDistrict: str
    correctDistrict: str
    addressDetails: str
    confidence: float = Field(ge=0, le=1)
    status: Status
    reason: str
    stateCode: str
    errorCode: str | None = None


class CorrectionResponse(BaseModel):
    companyName: str
    cases: list[CaseResponse]
