"""Remote OK source (https://remoteok.com/api).

REQUIRED ATTRIBUTION (from the API's own legal notice): link back to the job's
Remote OK URL (with a normal "follow" link, not nofollow) and mention Remote OK
as the source, or API access is suspended. Do not use the Remote OK logo.
We keep Remote OK's own URL in ``Job.url`` and set ``source="remoteok"``; the
README repeats this notice.

The API is one call returning the ~100 latest jobs (first element is the legal
notice). There is no search, so queries are matched locally.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Optional

from src.models import Job
from src.sources.base import JobSource, html_to_text, matches_query

log = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"
MIN_INTERVAL_SECONDS = 5.0


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


class RemoteOKSource(JobSource):
    name = "remoteok"

    def fetch(self, queries: list[str]) -> list[Job]:
        data = self._get_json_cached(API_URL, "remoteok", min_interval=MIN_INTERVAL_SECONDS)
        items = [d for d in data if isinstance(d, dict) and d.get("position")]  # drops the legal-notice element
        self.last_fetched_count = len(items)

        jobs: list[Job] = []
        for item in items:
            title = (item.get("position") or "").strip()
            description = html_to_text(item.get("description"))
            if not any(matches_query(q, f"{title}\n{description}") for q in queries):
                continue
            url = item.get("url")
            if not url:
                continue
            jobs.append(
                Job(
                    url=url,
                    title=title,
                    company=(item.get("company") or "").strip(),
                    location=(item.get("location") or "").strip(),
                    description=description,
                    source=self.name,
                    date_posted=_parse_date(item.get("date")),
                )
            )
        log.info("remoteok: %d of %d jobs match the configured queries", len(jobs), len(items))
        return jobs
