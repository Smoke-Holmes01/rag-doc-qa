"""Strict public and model-output contracts."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class Chunk(StrictModel):
    chunk_id: str
    filename: str
    title: str
    heading_path: list[str]
    ordinal: int = Field(ge=0)
    text: str = Field(min_length=1)
    content_hash: str


class QueryRequest(StrictModel):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra={"examples": [
        {"question": "根据星桥文档，配置修改后什么情况下需要重建索引？", "top_k": 4}]})
    question: str = Field(min_length=1, max_length=16000)
    top_k: int | None = Field(default=None, gt=0)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value

    @model_validator(mode="after")
    def explicit_top_k(self) -> "QueryRequest":
        if "top_k" in self.model_fields_set and self.top_k is None:
            raise ValueError("supplied top_k must be an integer")
        return self


class Claim(StrictModel):
    text: str = Field(min_length=1, max_length=2000)
    source_ids: list[str] = Field(min_length=1, max_length=8)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("empty claim")
        return value


class ModelAnswer(StrictModel):
    status: Literal["answered", "insufficient_evidence"]
    claims: list[Claim] = Field(max_length=12)
    reason: str = Field(max_length=500)

    @model_validator(mode="after")
    def consistent(self) -> "ModelAnswer":
        if self.status == "answered" and (not self.claims or self.reason):
            raise ValueError("answered requires claims and empty reason")
        if self.status == "insufficient_evidence" and (self.claims or not self.reason.strip()):
            raise ValueError("insufficient evidence requires reason and no claims")
        return self


class Citation(StrictModel):
    chunk_id: str
    filename: str
    title: str
    heading_path: list[str]
    text: str
    score: float


class QueryResponse(StrictModel):
    model_config = ConfigDict(strict=True, extra="forbid", json_schema_extra={"examples": [{
        "request_id": "example-request-id", "mode": "mock", "status": "insufficient_evidence",
        "answer": "资料不足：没有预算内的有效检索依据。", "claims": [], "citations": []}]})
    request_id: str
    mode: Literal["mock", "live"]
    status: Literal["answered", "insufficient_evidence"]
    answer: str
    claims: list[Claim]
    citations: list[Citation]


class ErrorDetail(StrictModel):
    code: str
    message: str
    request_id: str


class ErrorResponse(StrictModel):
    error: ErrorDetail


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    mode: Literal["mock", "live"]
    index_ready: bool
    reason: str | None
