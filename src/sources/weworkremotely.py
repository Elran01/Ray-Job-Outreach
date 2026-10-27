"""We Work Remotely source, via the public RSS feeds.

Terms/compliance: we only read the RSS feeds WWR publishes for this purpose
(robots.txt allows them; no HTML scraping, no search endpoints, which return
406 to bots). Job URLs point back to weworkremotely.com and ``source`` is
``weworkremotely``. Feeds are fetched once per run, cached on disk, and
requested with a delay between them.

Feeds are listed in config.yaml (sources.weworkremotely.feeds). RSS has no
search, so the configured queries are matched locally against title + body.
WWR titles look like "Company: Job Title"; the company is split off the front.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser

from src.models import Job
from src.sources.base import JobSource, html_to_text, matches_query

log = logging.getLogger(__name__)

FEED_BASE = "https://weworkremotely.com"
MIN_INTERVAL_SECONDS = 5.0


def _parse_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()
    except (TypeError, ValueError):
        return None


def _split_company_title(raw: str) -> tuple[str, str]:
    """'Acme: Executive Assistant' -> ('Acme', 'Executive Assistant')."""
    company, sep, title = raw.partition(": ")
    if not sep:
        return "", raw.strip()
    return company.strip(), title.strip()


class WeWorkRemotelySource(JobSource):
    name = "weworkremotely"

    def fetch(self, queries: list[str]) -> list[Job]:
        feeds: list[str] = self.options.get("feeds") or []
        if not feeds:
            raise ValueError("weworkremotely: no feeds configured")

        jobs: list[Job] = []
        failures = 0
        for feed_path in feeds:
            url = feed_path if feed_path.startswith("http") else f"{FEED_BASE}{feed_path}"
            cache_key = "wwr_" + re.sub(r"\W+", "_", feed_path)
            try:
                content = self._get_cached_bytes(url, cache_key, MIN_INTERVAL_SECONDS)
                if not content.strip():
                    raise ValueError("empty response")
                parsed = feedparser.parse(content)
                if parsed.bozo and not parsed.entries:  # e.g. an HTML error page served with HTTP 200
                    raise ValueError(f"not a valid RSS feed ({parsed.get('bozo_exception')})")
            except Exception as exc:  # one bad feed must not lose the others
                failures += 1
                if self.cache_dir:  # don't let a bad response poison the cache
                    (self.cache_dir / f"{cache_key}.cache").unlink(missing_ok=True)
                log.error("weworkremotely: feed %s failed: %s", feed_path, exc)
                continue
            self.last_fetched_count += len(parsed.entries)
            jobs.extend(self._entries_to_jobs(parsed.entries, queries))
            log.info("weworkremotely: %s -> %d entries", feed_path, len(parsed.entries))

        if failures == len(feeds):
            raise RuntimeError("weworkremotely: all feeds failed")
        log.info("weworkremotely: %d jobs match the configured queries (before dedupe)", len(jobs))
        return jobs

    def _entries_to_jobs(self, entries, queries: list[str]) -> list[Job]:
        out: list[Job] = []
        for e in entries:
            company, title = _split_company_title(e.get("title", ""))
            description = html_to_text(e.get("summary"))
            if not any(matches_query(q, f"{title}\n{description}") for q in queries):
                continue
            link = e.get("link")
            if not link or not title:
                continue
            location = ", ".join(p for p in (e.get("region", ""), e.get("state", ""), e.get("country", "")) if p)
            out.append(
                Job(
                    url=link,
                    title=title,
                    company=company,
                    location=location,
                    description=description,
                    source=self.name,
                    date_posted=_parse_date(e.get("published")),
                )
            )
        return out
