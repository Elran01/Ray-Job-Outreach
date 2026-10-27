"""Stage 1: keyword prefilter (free). Stage 2: LLM validation (PRD 4.2)."""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from pydantic import ValidationError

from src.models import Job, ValidationResult

log = logging.getLogger(__name__)

Message = dict  # {"role": "user"|"assistant", "content": str}
# A completion function: takes the chat messages, returns the model's text reply.
Complete = Callable[[list[Message]], str]


# ---------------------------------------------------------------- prefilter
def _kw_pattern(keyword: str) -> re.Pattern:
    # whole word, allowing a plural / -ing ending: "engineer" hits "engineers" and
    # "engineering" but "sales" does not hit "salesforce".
    return re.compile(r"(?<!\w)" + re.escape(keyword.lower()) + r"(?:s|es|ing)?(?!\w)")


def prefilter(
    job: Job, excluded: list[str], protected: Optional[list[str]] = None
) -> Optional[str]:
    """Return a drop reason, or None to keep the job.

    ``protected`` keywords (e.g. "assistant") exempt a title from exclusion so
    that "Executive Assistant to the VP of Sales" still reaches the LLM. Those
    ambiguous titles ("Sales Admin Manager") are the LLM's job to judge.
    """
    title = job.title.lower()
    if any(_kw_pattern(p).search(title) for p in (protected or [])):
        return None
    for kw in excluded:
        if _kw_pattern(kw).search(title):
            return f"title contains excluded keyword '{kw}'"
    return None


# ---------------------------------------------------------- LLM validation
def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = int(limit * 0.75)
    return text[:head] + "\n[... truncated ...]\n" + text[-(limit - head):]


def render_prompt(template: str, job: Job, max_chars: int = 6000) -> str:
    """Fill {{variables}}. The description goes last so its text is never re-expanded."""
    out = template
    for key, value in {
        "{{job_title}}": job.title,
        "{{location}}": job.location or "not specified",
        "{{source}}": job.source,
    }.items():
        out = out.replace(key, value)
    return out.replace("{{job_description}}", _truncate(job.description, max_chars))


def parse_validation(text: str) -> ValidationResult:
    """Extract the JSON object from a reply and validate it. Raises ValueError."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in reply")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    try:
        return ValidationResult.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"schema error: {exc.errors(include_url=False)}") from exc


# Errors that mean the setup is wrong (not that one job is bad): abort the run.
FATAL_ERRORS = {"AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError", "TypeError"}


class ValidationFailure(Exception):
    """Reply could not be parsed/validated even after the retry."""


def validate_job(job: Job, template: str, complete: Complete, max_chars: int = 6000) -> ValidationResult:
    """Ask the LLM; on a bad reply retry once; then raise ValidationFailure."""
    messages: list[Message] = [{"role": "user", "content": render_prompt(template, job, max_chars)}]
    reply = complete(messages)
    try:
        return parse_validation(reply)
    except ValueError as first_error:
        log.warning("validation parse failure for %s (%s); retrying once", job.url, first_error)
        messages += [
            {"role": "assistant", "content": reply},
            {
                "role": "user",
                "content": f"That reply was not usable ({first_error}). Reply again with ONLY the JSON "
                "object described above: real booleans for the three is_* fields, strings for "
                "language_requirements and reasoning.",
            },
        ]
        reply = complete(messages)
        try:
            return parse_validation(reply)
        except ValueError as second_error:
            raise ValidationFailure(str(second_error)) from second_error


def anthropic_completer(api_key: str, model: str, temperature: Optional[float], max_tokens: int) -> Complete:
    """Build a completion function.

    ``temperature`` is only sent when set. The current Anthropic API/SDK rejects the
    parameter ("temperature: Extra inputs are not permitted"), so config.yaml leaves
    it null; set a value only if your model/API version accepts it.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key, max_retries=4)  # SDK backs off on 429/5xx
    extra = {"extra_body": {"temperature": temperature}} if temperature is not None else {}

    def complete(messages: list[Message]) -> str:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages,
            **extra,
        )
        return "".join(block.text for block in resp.content if block.type == "text")

    return complete


# ------------------------------------------------------------- batch driver
@dataclass
class Decision:
    job: Job
    stage: str  # "prefilter" | "llm" | "llm_error"
    passed: bool
    result: Optional[ValidationResult] = None
    reason: str = ""

    @property
    def failed_criteria(self) -> str:
        if self.result:
            return ",".join(self.result.failed_criteria)
        return ""


def run_prefilter(jobs: list[Job], excluded: list[str], protected: list[str]) -> tuple[list[Job], list[Decision]]:
    kept: list[Job] = []
    dropped: list[Decision] = []
    for job in jobs:
        reason = prefilter(job, excluded, protected)
        if reason:
            dropped.append(Decision(job=job, stage="prefilter", passed=False, reason=reason))
        else:
            kept.append(job)
    return kept, dropped


def run_validation(
    jobs: list[Job], template: str, complete: Complete, max_chars: int = 6000, workers: int = 4
) -> list[Decision]:
    """Validate jobs concurrently; never raises for a single bad job."""

    def one(job: Job) -> Decision:
        try:
            result = validate_job(job, template, complete, max_chars)
        except ValidationFailure as exc:
            log.error("giving up on %s: invalid LLM JSON after retry: %s", job.url, exc)
            return Decision(job, "llm_error", False, reason=f"LLM reply could not be parsed after retry: {exc}")
        except Exception as exc:  # network/API failure after SDK retries
            if type(exc).__name__ in FATAL_ERRORS:
                raise  # bad key/model/request shape: fail fast instead of one error per job
            log.error("LLM call failed for %s: %s", job.url, exc)
            return Decision(job, "llm_error", False, reason=f"LLM call failed: {type(exc).__name__}: {exc}")
        reason = result.reasoning if not result.passed else ""
        return Decision(job, "llm", result.passed, result=result, reason=reason)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, jobs))


def load_template(path: Path) -> str:
    return path.read_text(encoding="utf-8")
