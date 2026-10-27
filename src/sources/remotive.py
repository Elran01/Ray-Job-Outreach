"""Remotive source (https://remotive.com/api/remote-jobs).

Terms (https://github.com/remotive-com/remote-jobs-api):
- Link back to the Remotive job URL and mention Remotive as the source.
  We keep Remotive's own URL in ``Job.url`` and set ``source="remotive"``.
- Do not republish jobs to third-party job sites. Output stays in a private sheet.
- Rate limit: max ~4 calls/day advised, >2/min is blocked. We make ONE call per
  run (cached on disk) and match the queries locally.

Observed behaviour (checked while building): the public endpoint currently
returns a fixed set of ~10 jobs and ignores the ``search``, ``category`` and
``limit`` parameters, so query matching is done client-side.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from src.models import Job
from src.sources.base import JobSource, html_to_text, matches_query

log = logging.getLogger(__name__)

API_URL = "https://remotive.com/api/remote-jobs"
MIN_INTERVAL_SECONDS = 35.0  # Remotive blocks > 2 requests/minute


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


class RemotiveSource(JobSource):
    name = "remotive"

    def fetch(self, queries: list[str]) -> list[Job]:
        data = self._get_json_cached(API_URL, "remotive", min_interval=MIN_INTERVAL_SECONDS)
        items = data.get("jobs", [])
        self.last_fetched_count = len(items)
        log.info("remotive: API returned %d jobs", len(items))

        jobs: list[Job] = []
        for item in items:
            title = (item.get("title") or "").strip()
            description = html_to_text(item.get("description"))
            if not any(matches_query(q, f"{title}\n{description}") for q in queries):
                continue
            url = item.get("url")
            if not url or not title:
                log.warning("remotive: skipping item without url/title: %s", item.get("id"))
                continue
            jobs.append(
                Job(
                    url=url,
                    title=title,
                    company=(item.get("company_name") or "").strip(),
                    location=(item.get("candidate_required_location") or "").strip(),
                    description=description,
                    language_requirements=None,  # extracted by the LLM in Phase 4
                    source=self.name,
                    date_posted=_parse_date(item.get("publication_date")),
                )
            )
        log.info("remotive: %d jobs match the configured queries", len(jobs))
        return jobs
