"""Save raw/filtered data and dedupe jobs."""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from src.models import Job

# Query parameters that only carry tracking info, never identify a posting.
TRACKING_PARAMS = {
    "ref", "ref_src", "source", "src", "fbclid", "gclid", "mc_cid", "mc_eid",
    "gh_src", "gh_jid", "lever-source", "lever-origin", "trk", "trkid", "campaign",
}


def normalize_url(url: str) -> str:
    """Canonical form used as the dedupe key.

    Lowercases scheme/host, strips 'www.', default ports, fragments, tracking
    params (utm_*, ref, ...) and trailing slashes; sorts remaining params.
    """
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if parts.port and not (scheme, parts.port) in {("http", 80), ("https", 443)}:
        host = f"{host}:{parts.port}"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS
    )
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/")
    return urlunsplit(("https" if scheme in ("http", "https") else scheme, host, path, urlencode(query), ""))


def _norm_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def company_title_key(job: Job) -> str | None:
    """Fallback key; None when company is unknown (too risky to merge on title alone)."""
    company, title = _norm_text(job.company), _norm_text(job.title)
    if not company or not title:
        return None
    return f"{company}|{title}"


@dataclass
class Duplicate:
    dropped: Job
    kept: Job
    reason: str  # "url" or "company+title"


def dedupe(jobs: list[Job]) -> tuple[list[Job], list[Duplicate]]:
    """Remove duplicates: normalized URL first, then company+title.

    First occurrence wins (keeps source order stable); if a later duplicate has
    a longer description, only the description is upgraded.
    """
    by_url: dict[str, int] = {}
    by_ct: dict[str, int] = {}
    unique: list[Job] = []
    dups: list[Duplicate] = []

    for job in jobs:
        url_key = normalize_url(job.url)
        ct_key = company_title_key(job)
        idx, reason = None, ""
        if url_key in by_url:
            idx, reason = by_url[url_key], "url"
        elif ct_key and ct_key in by_ct:
            idx, reason = by_ct[ct_key], "company+title"

        if idx is None:
            by_url[url_key] = len(unique)
            if ct_key:
                by_ct[ct_key] = len(unique)
            unique.append(job)
            continue

        kept = unique[idx]
        dups.append(Duplicate(dropped=job, kept=kept, reason=reason))
        if len(job.description) > len(kept.description):
            unique[idx] = kept.model_copy(update={"description": job.description})
        by_url.setdefault(url_key, idx)
    return unique, dups


def save_raw_json(jobs: list[Job], path: Path) -> None:
    """Write everything collected to a JSON file (list of Job dicts)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [job.model_dump(mode="json") for job in jobs]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


FILTERED_COLUMNS = [
    "url", "title", "company", "location", "description", "language_requirements",
    "source", "date_posted", "date_collected", "validation_reasoning",
]
REJECTED_COLUMNS = [
    "url", "title", "company", "location", "source", "stage", "failed_criteria",
    "is_remote", "is_english_speaking", "is_relevant_role", "language_requirements", "reason",
]


def _write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel reads UTF-8
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def save_filtered_csv(decisions: list, path: Path) -> None:
    """Passed jobs (Decision objects from src.filter) with extracted language requirements."""
    rows = []
    for d in decisions:
        job = d.job.model_copy(update={"language_requirements": d.result.language_requirements})
        row = job.model_dump(mode="json")
        row["validation_reasoning"] = d.result.reasoning
        rows.append(row)
    _write_csv(path, FILTERED_COLUMNS, rows)


def save_rejected_csv(decisions: list, path: Path) -> None:
    """Rejected jobs, one clear reason per row."""
    rows = []
    for d in decisions:
        row = {
            "url": d.job.url, "title": d.job.title, "company": d.job.company,
            "location": d.job.location, "source": d.job.source, "stage": d.stage,
            "failed_criteria": d.failed_criteria, "reason": d.reason,
        }
        if d.result:
            row.update(
                is_remote=d.result.is_remote,
                is_english_speaking=d.result.is_english_speaking,
                is_relevant_role=d.result.is_relevant_role,
                language_requirements=d.result.language_requirements,
            )
        rows.append(row)
    _write_csv(path, REJECTED_COLUMNS, rows)

