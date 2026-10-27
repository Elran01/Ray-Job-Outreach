"""Outreach message generation (PRD 4.3).

The prompt is a plain-text file with ``{{variables}}``; swapping tone means editing or
choosing a different file (``--outreach-prompt PATH``), never touching code.
"""
from __future__ import annotations

import csv
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.filter import Complete, Message, _truncate
from src.models import Job

log = logging.getLogger(__name__)

TEMPLATE_VARIABLES = ("job_title", "company", "location", "job_description", "sender_profile")

_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# Phrases the PRD forbids; if one slips through we retry once.
BANNED_PHRASES = (
    "hope this email finds you well", "hope this finds you well", "hope you're well", "hope you are well",
    # unprovable claims about the sender's candidates (PRD: no fake claims about the sender)
    "pre-vetted", "pre-screen", "prevetted", "top-tier", "we specialize", "we've got", "we have candidates",
)
PLACEHOLDER_RE = re.compile(r"\[[^\]\n]{1,40}\]|\{\{|\}\}")


# ------------------------------------------------------------------ prompt
def load_prompt(path: Path) -> str:
    """Read a prompt file, drop <!-- comments -->, and fail early on unknown {{variables}}."""
    text = _COMMENT_RE.sub("", path.read_text(encoding="utf-8")).strip()
    unknown = sorted({m for m in _VAR_RE.findall(text) if m not in TEMPLATE_VARIABLES})
    if unknown:
        raise ValueError(
            f"{path}: unknown template variable(s) {unknown}; allowed: "
            + ", ".join("{{%s}}" % v for v in TEMPLATE_VARIABLES)
        )
    return text


def load_sender_profile(path: Path) -> str:
    return _COMMENT_RE.sub("", path.read_text(encoding="utf-8")).strip()


def render_outreach_prompt(template: str, job: Job, sender_profile: str, max_desc_chars: int = 4000) -> str:
    """Fill the variables in ONE pass so text inside a job description is never re-expanded."""
    values = {
        "job_title": job.title,
        "company": job.company or "the company",
        "location": job.location or "not specified",
        "job_description": _truncate(job.description, max_desc_chars),
        "sender_profile": sender_profile,
    }
    return _VAR_RE.sub(lambda m: values[m.group(1)], template)


# ------------------------------------------------------------- generation
def clean_message(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-z]*\s*|\s*```$", "", text).strip()
    if len(text) > 1 and text[0] == text[-1] == '"':
        text = text[1:-1].strip()
    return text


def word_count(text: str) -> int:
    return len(text.split())


def check_message(text: str, max_words: int) -> list[str]:
    """Return a list of guardrail problems (empty = fine)."""
    problems = []
    if not text:
        return ["empty reply"]
    if word_count(text) > max_words:
        problems.append(f"too long ({word_count(text)} words, limit {max_words})")
    low = text.lower()
    if any(p in low for p in BANNED_PHRASES):
        hits = [p for p in BANNED_PHRASES if p in low]
        problems.append("contains banned wording: " + ", ".join(f"'{p}'" for p in hits))
    if PLACEHOLDER_RE.search(text):
        problems.append("contains an unfilled placeholder like [Name]")
    return problems


@dataclass
class OutreachResult:
    job: Job
    message: str
    prompt_file: str  # which prompt file produced this message (PRD 4.3)
    model: str = ""
    flags: list[str] = field(default_factory=list)  # guardrail problems that remained after retry
    error: str = ""

    @property
    def words(self) -> int:
        return word_count(self.message)


def generate_message(
    job: Job,
    template: str,
    sender_profile: str,
    complete: Complete,
    max_words: int = 130,
    max_desc_chars: int = 4000,
) -> tuple[str, list[str]]:
    """One LLM call; if a guardrail trips, retry once with feedback. Returns (message, problems)."""
    messages: list[Message] = [
        {"role": "user", "content": render_outreach_prompt(template, job, sender_profile, max_desc_chars)}
    ]
    reply = clean_message(complete(messages))
    problems = check_message(reply, max_words)
    if not problems:
        return reply, []
    log.warning("outreach guardrail for %s: %s; retrying once", job.url, "; ".join(problems))
    messages += [
        {"role": "assistant", "content": reply},
        {
            "role": "user",
            "content": "Rewrite the message fixing these problems: " + "; ".join(problems)
            + ". Keep every other rule. Output ONLY the message text.",
        },
    ]
    retry = clean_message(complete(messages))
    return retry, check_message(retry, max_words)


def run_outreach(
    jobs: list[Job],
    prompt_paths: list[Path],
    sender_profile: str,
    complete: Complete,
    model: str = "",
    max_words: int = 130,
    max_desc_chars: int = 4000,
    workers: int = 4,
    root: Optional[Path] = None,
) -> list[OutreachResult]:
    """Generate one message per (prompt file, job). Results are ordered prompt-by-prompt."""
    tasks: list[tuple[Job, str, str]] = []
    for path in prompt_paths:
        template = load_prompt(path)  # raises on unknown variables before any API spend
        label = path.relative_to(root).as_posix() if root and path.is_relative_to(root) else path.as_posix()
        tasks.extend((job, template, label) for job in jobs)

    def one(task: tuple[Job, str, str]) -> OutreachResult:
        job, template, label = task
        try:
            msg, problems = generate_message(job, template, sender_profile, complete, max_words, max_desc_chars)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ in {"AuthenticationError", "PermissionDeniedError", "NotFoundError", "BadRequestError"}:
                raise  # wrong key/model: fail fast instead of one error per job
            log.error("outreach failed for %s with %s: %s", job.url, label, exc)
            return OutreachResult(job, "", label, model, error=f"{type(exc).__name__}: {exc}")
        return OutreachResult(job, msg, label, model, flags=problems)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, tasks))


# --------------------------------------------------------------- storage
OUTREACH_COLUMNS = [
    "url", "title", "company", "location", "outreach_message", "prompt_used", "model", "words", "flags", "error",
]


def save_outreach_csv(results: list[OutreachResult], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=OUTREACH_COLUMNS)
        w.writeheader()
        for r in results:
            w.writerow({
                "url": r.job.url, "title": r.job.title, "company": r.job.company, "location": r.job.location,
                "outreach_message": r.message, "prompt_used": r.prompt_file, "model": r.model,
                "words": r.words, "flags": "; ".join(r.flags), "error": r.error,
            })


def load_outreach_csv(path: Path, jobs: list[Job]) -> list[OutreachResult]:
    """Read data/jobs_outreach.csv back into results (jobs matched by URL for the full record)."""
    by_url = {j.url: j for j in jobs}
    results = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            job = by_url.get(row["url"]) or Job(
                url=row["url"], title=row["title"], company=row["company"], location=row["location"], source="unknown"
            )
            results.append(OutreachResult(
                job, row["outreach_message"], row["prompt_used"], row.get("model", ""),
                flags=[x for x in row.get("flags", "").split("; ") if x], error=row.get("error", ""),
            ))
    return results


def load_filtered_jobs(path: Path) -> list[Job]:
    """Read data/jobs_filtered.csv (Phase 4 output) back into Job objects."""
    jobs = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            data = {
                "url": row["url"], "title": row["title"], "company": row["company"],
                "location": row["location"], "description": row["description"],
                "language_requirements": row.get("language_requirements") or None,
                "source": row["source"], "date_posted": row.get("date_posted") or None,
            }
            if row.get("date_collected"):
                data["date_collected"] = row["date_collected"]
            jobs.append(Job(**data))
    return jobs
