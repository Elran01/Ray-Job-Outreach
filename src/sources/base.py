"""JobSource interface plus shared HTTP and HTML helpers."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

from src.models import Job

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 20  # seconds
RETRY_STATUS = {429, 500, 502, 503, 504}


def html_to_text(html: Optional[str]) -> str:
    """Convert an HTML fragment to clean plain text.

    Block elements become line breaks, list items become "- " bullets,
    entities are decoded, and whitespace is normalised.
    """
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
    for block in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr"]):
        block.insert_before("\n")
        block.insert_after("\n")
    text = soup.get_text()
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def matches_query(query: str, haystack: str) -> bool:
    """Case-insensitive whole-phrase match ('ea to ceo' must not match 'idea to ceo')."""
    pattern = r"(?<!\w)" + re.escape(query.lower()) + r"(?!\w)"
    return re.search(pattern, haystack.lower()) is not None


class JobSource(ABC):
    """Interface every job source implements: fetch(queries) -> list[Job]."""

    name: str = "base"

    def __init__(self, http_cfg: dict, cache_dir: Optional[Path] = None, options: Optional[dict] = None):
        self.options: dict = options or {}
        self.last_fetched_count = 0  # items returned by the source before query matching
        self.user_agent: str = http_cfg["user_agent"]
        self.delay: float = float(http_cfg.get("request_delay_seconds", 1.0))
        self.max_retries: int = int(http_cfg.get("max_retries", 3))
        self.timeout: float = float(http_cfg.get("timeout_seconds", DEFAULT_TIMEOUT))
        self.cache_ttl: float = float(http_cfg.get("cache_ttl_minutes", 0)) * 60
        self.cache_dir = cache_dir
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": self.user_agent})
        self._last_request = 0.0
        self._lock = threading.Lock()

    # -- to implement ---------------------------------------------------
    @abstractmethod
    def fetch(self, queries: list[str]) -> list[Job]:
        """Return jobs matching any of the queries."""

    # -- helpers --------------------------------------------------------
    def _throttle(self, min_interval: float) -> None:
        with self._lock:
            wait = self._last_request + min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

    def _get(self, url: str, params: Optional[dict] = None, min_interval: Optional[float] = None) -> requests.Response:
        """GET with timeout, rate limiting and exponential backoff on 429/5xx."""
        interval = self.delay if min_interval is None else min_interval
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            self._throttle(interval)
            try:
                resp = self._session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                log.warning("%s: request error (%s), attempt %d", self.name, exc, attempt + 1)
            else:
                if resp.status_code not in RETRY_STATUS:
                    resp.raise_for_status()
                    if resp.status_code >= 300:  # unresolved redirect etc. is not a usable response
                        raise requests.HTTPError(f"unexpected HTTP {resp.status_code}", response=resp)
                    return resp
                last_exc = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
                log.warning("%s: HTTP %s, attempt %d", self.name, resp.status_code, attempt + 1)
            if attempt < self.max_retries:
                time.sleep(2 ** attempt * 2)
        raise RuntimeError(f"{self.name}: giving up on {url}: {last_exc}")

    def _get_json_cached(self, url: str, cache_key: str, min_interval: Optional[float] = None) -> Any:
        return json.loads(self._get_cached_bytes(url, cache_key, min_interval))

    def _get_cached_bytes(self, url: str, cache_key: str, min_interval: Optional[float] = None) -> bytes:
        """GET a URL, reusing a recent on-disk copy if cache_ttl_minutes > 0."""
        path = self.cache_dir / f"{cache_key}.cache" if self.cache_dir else None
        if path and self.cache_ttl > 0 and path.exists():
            age = time.time() - path.stat().st_mtime
            if age < self.cache_ttl:
                log.info("%s: using cached response for %s (%.0f min old)", self.name, cache_key, age / 60)
                return path.read_bytes()
        content = self._get(url, min_interval=min_interval).content
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        return content
