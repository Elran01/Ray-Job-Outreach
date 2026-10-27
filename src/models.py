"""Core data models."""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(BaseModel):
    """A single job posting collected from any source."""

    url: str
    title: str
    company: str
    location: str = ""
    description: str = ""  # plain text, HTML stripped
    language_requirements: Optional[str] = None  # extracted during validation
    source: str
    date_posted: Optional[date] = None
    date_collected: datetime = Field(default_factory=_now)


class ValidationResult(BaseModel):
    """Strict schema for the LLM validation reply (PRD 4.2)."""

    model_config = ConfigDict(extra="ignore")

    is_remote: StrictBool  # real JSON booleans only; "true" (string) is rejected
    is_english_speaking: StrictBool
    is_relevant_role: StrictBool
    language_requirements: str
    reasoning: str

    @field_validator("reasoning", "language_requirements", mode="before")
    @classmethod
    def _flatten(cls, v):
        # Tolerate the model returning a dict/list of short sentences.
        if isinstance(v, dict):
            return " ".join(f"{k}: {x}" for k, x in v.items())
        if isinstance(v, list):
            return "; ".join(str(x) for x in v)
        return v

    @property
    def passed(self) -> bool:
        return self.is_remote and self.is_english_speaking and self.is_relevant_role

    @property
    def failed_criteria(self) -> list[str]:
        checks = {
            "is_remote": self.is_remote,
            "is_english_speaking": self.is_english_speaking,
            "is_relevant_role": self.is_relevant_role,
        }
        return [name for name, ok in checks.items() if not ok]
