"""Request and response schemas (pydantic v2)."""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Classification = Literal["public", "internal", "confidential", "restricted"]
Availability = Literal["99.0", "99.5", "99.9", "99.95", "99.99"]
ServerName = Literal["enterprise-standards", "approved-service-catalog"]

MAX_BODY_BYTES = 8192
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _clean(s: str) -> str:
    return _CTRL.sub("", s).strip()


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=3, max_length=120)
    description: str = Field(min_length=40, max_length=4000)
    business_objective: str = Field(min_length=10, max_length=1000)
    data_classification: Classification
    expected_users: int = Field(ge=1, le=10_000_000)
    availability_target: Availability
    external_integrations: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("title", "description", "business_objective")
    @classmethod
    def _strip_ctrl(cls, v: str) -> str:
        return _clean(v)

    @field_validator("external_integrations")
    @classmethod
    def _integrations(cls, v: list[str]) -> list[str]:
        out = []
        for item in v:
            item = _clean(str(item))
            if not item:
                continue
            if len(item) > 100:
                raise ValueError("each integration must be at most 100 characters")
            out.append(item)
        return out


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(min_length=6, max_length=64)
    role: Literal["architect", "governance"]


# ---------- Structured review output ----------

class SourceRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    server: ServerName
    record_id: str = Field(max_length=40)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    verdict: Literal["APPROVE_WITH_CONTROLS", "NEEDS_CHANGES", "NOT_RECOMMENDED", "INSUFFICIENT_INFORMATION"]
    rationale: str = Field(max_length=2000)


class Component(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service_id: str = Field(max_length=40)
    name: str = Field(default="", max_length=120)
    role: str = Field(max_length=400)
    catalog_status: str = Field(default="unknown", max_length=40)
    constraints_to_apply: list[str] = Field(default_factory=list, max_length=8)
    source: SourceRef | None = None


class PatternRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    pattern_id: str = Field(max_length=40)
    name: str = Field(default="", max_length=120)
    fit: str = Field(max_length=600)
    source: SourceRef | None = None


class Architecture(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str = Field(max_length=2000)
    components: list[Component] = Field(default_factory=list, max_length=20)
    reference_patterns: list[PatternRef] = Field(default_factory=list, max_length=5)


class StandardRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    standard_id: str = Field(max_length=40)
    title: str = Field(default="", max_length=200)
    relevance: str = Field(max_length=800)
    source: SourceRef | None = None


class Risk(BaseModel):
    model_config = ConfigDict(extra="ignore")
    category: Literal["security", "privacy", "resiliency", "operational", "cost", "compliance", "ai_safety"]
    severity: Literal["low", "medium", "high", "critical"]
    description: str = Field(max_length=800)
    basis: Literal["retrieved_fact", "model_recommendation"]
    sources: list[SourceRef] = Field(default_factory=list, max_length=6)


class ControlRef(BaseModel):
    model_config = ConfigDict(extra="ignore")
    control_id: str = Field(max_length=40)
    title: str = Field(default="", max_length=200)
    how_to_apply: str = Field(default="", max_length=600)
    standard_id: str = Field(default="", max_length=40)
    source: SourceRef | None = None


class Conflict(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str = Field(max_length=800)
    sources: list[SourceRef] = Field(default_factory=list, max_length=6)
    suggested_resolution: str = Field(max_length=800)


class ModelReview(BaseModel):
    """What the model must return through the submit_review tool."""
    model_config = ConfigDict(extra="ignore")
    executive_summary: str = Field(max_length=2500)
    recommendation: Recommendation
    architecture: Architecture
    standards: list[StandardRef] = Field(default_factory=list, max_length=20)
    risks: list[Risk] = Field(default_factory=list, max_length=20)
    controls: list[ControlRef] = Field(default_factory=list, max_length=40)
    conflicts: list[Conflict] = Field(default_factory=list, max_length=10)
    open_questions: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("open_questions")
    @classmethod
    def _q(cls, v: list[str]) -> list[str]:
        return [q[:400] for q in v if q.strip()]


DISCLAIMER = (
    "ADVISORY ONLY. This review was generated by an AI assistant using SYNTHETIC demonstration standards and a "
    "SYNTHETIC service catalog for a fictional organization. It is not an approval, does not reflect the policies, "
    "systems, or approvals of any real company, and must be validated by a qualified human reviewer."
)
