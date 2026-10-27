"""Himalayas source (https://himalayas.app/docs/remote-jobs-api).

Terms (checked while building): no key needed (a free key raises limits to
60 req/min, 10k/day). We must show a visible Himalayas source credit and link
back, and must NOT pass jobs on to other job boards/aggregators/Google Jobs/
LinkedIn Jobs. Output stays in a private sheet. ``Job.url`` is the Himalayas
job page and ``source`` is ``himalayas``; the README repeats the credit.

Unlike the other sources this API has real server-side search
(``/jobs/api/search?q=...``, 20 results per page, ``page=N``), so each configured
query is searched directly. Matching is fuzzy, so relevance is left to the
prefilter + LLM validation stages.
"""
from __future__ import annotations

import logging
import os
import re
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import quote_plus

from src.models import Job
from src.sources.base import JobSource, html_to_text

log = logging.getLogger(__name__)

SEARCH_URL = "https://himalayas.app/jobs/api/search"
PAGE_SIZE = 20  # API maximum
DEFAULT_MAX_PAGES = 3
MIN_INTERVAL_SECONDS = 1.5


def _epoch_to_date(value) -> Optional[date]:
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).date()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class HimalayasSource(JobSource):
    name = "himalayas"

    def fetch(self, queries: list[str]) -> list[Job]:
        max_pages = int(self.options.get("max_pages_per_query", DEFAULT_MAX_PAGES))
        api_key = os.getenv("HIMALAYAS_API_KEY")
        if api_key:
            self._session.headers["X-API-Key"] = api_key

        jobs: list[Job] = []
        errors = 0
        for query in queries:
            for page in range(1, max_pages + 1):
                key = "himalayas_" + re.sub(r"\W+", "_", query.lower()) + f"_p{page}"
                url = f"{SEARCH_URL}?q={quote_plus(query)}&limit={PAGE_SIZE}&page={page}"
                try:
                    data = self._get_json_cached(url, key, min_interval=MIN_INTERVAL_SECONDS)
                except Exception as exc:  # a failed query/page must not lose the others
                    errors += 1
                    log.error("himalayas: query %r page %d failed: %s", query, page, exc)
                    break
                items = data.get("jobs", [])
                self.last_fetched_count += len(items)
                jobs.extend(j for j in (self._to_job(i) for i in items) if j)
                if len(items) < PAGE_SIZE:
                    break  # last page for this query
        if errors and not jobs:
            raise RuntimeError(f"himalayas: all requests failed ({errors} errors)")
        log.info("himalayas: %d results across %d queries (before dedupe)", len(jobs), len(queries))
        return jobs

    def _to_job(self, item: dict) -> Optional[Job]:
        url = item.get("guid") or item.get("applicationLink")
        title = (item.get("title") or "").strip()
        if not url or not title:
            return None
        restrictions = item.get("locationRestrictions") or []
        # Himalayas is remote-only; an empty restriction list means "work from anywhere".
        location = ", ".join(restrictions) if restrictions else "Worldwide"
        return Job(
            url=url,
            title=title,
            company=(item.get("companyName") or "").strip(),
            location=location,
            description=html_to_text(item.get("description")),
            source=self.name,
            date_posted=_epoch_to_date(item.get("pubDate")),
        )
