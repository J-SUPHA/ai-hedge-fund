"""Throttled, retrying, disk-cached HTTP for scraped sources.

Politeness is per host (SEC asks for <= 10 requests/second and a declared
User-Agent with a contact address; Yahoo throttles bursts). Responses are
cached on disk keyed by URL; callers choose a TTL (None = forever, for
immutable things like a past year of prices or a filed Form 4).

Fail loud: after retries, an infrastructure failure raises OpenDataError.
A 404 returns None (the resource genuinely does not exist).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from hedge_fund.paths import CACHE_DIR, write_atomic

DEFAULT_CACHE = CACHE_DIR / "opendata"
MIN_INTERVAL = {  # seconds between requests to one host
    "www.sec.gov": 0.15,
    "data.sec.gov": 0.15,
    "query1.finance.yahoo.com": 0.4,
    "query2.finance.yahoo.com": 0.4,
}
DEFAULT_INTERVAL = 1.0
# Yahoo 429s full browser strings and python-requests, but serves the bare token.
BROWSER_UA = "Mozilla/5.0"


class OpenDataError(RuntimeError):
    """A source could not be reached or returned garbage after retries."""


def sec_user_agent() -> str:
    """SEC fair-access policy: identify yourself with a contact address."""
    explicit = os.environ.get("AIHF_SEC_USER_AGENT")
    if explicit:
        return explicit
    try:
        email = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True,
                               timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        email = ""
    return f"aihf-lab research {email or 'set AIHF_SEC_USER_AGENT'}"


class Fetcher:
    def __init__(self, cache_dir: Path | None = None, *, offline: bool = False) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else DEFAULT_CACHE
        self.offline = offline  # tests: cache only, never the network
        self._session = requests.Session()
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()
        self.requests_made = 0

    def _headers(self, host: str) -> dict[str, str]:
        if host.endswith("sec.gov"):
            return {"User-Agent": sec_user_agent(), "Accept-Encoding": "gzip, deflate"}
        return {"User-Agent": BROWSER_UA}

    def _path(self, url: str) -> Path:
        host = urlparse(url).netloc
        return self.cache_dir / host / (hashlib.sha256(url.encode()).hexdigest()[:32] + ".json")

    def _throttle(self, host: str) -> None:
        with self._lock:
            wait = MIN_INTERVAL.get(host, DEFAULT_INTERVAL) - (time.monotonic() - self._last.get(host, 0.0))
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def get(self, url: str, *, ttl: float | None, kind: str = "json") -> Any:
        """Fetch *url* (JSON or text). ttl=None caches forever."""
        path = self._path(url)
        if path.exists():
            try:
                hit = json.loads(path.read_text())
                if ttl is None or time.time() - hit["fetched_at"] < ttl:
                    return hit["body"]
            except (json.JSONDecodeError, KeyError, OSError):
                pass
        if self.offline:
            raise OpenDataError(f"offline and not cached: {url}")

        host = urlparse(url).netloc
        last_error = None
        for attempt in range(5):
            self._throttle(host)
            try:
                resp = self._session.get(url, headers=self._headers(host), timeout=30)
                self.requests_made += 1
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 404:
                body = None
            elif resp.status_code in (429, 500, 502, 503, 504):
                last_error = f"HTTP {resp.status_code}"
                time.sleep(2 ** attempt + 1)
                continue
            elif resp.status_code != 200:
                raise OpenDataError(f"{url}: HTTP {resp.status_code}: {resp.text[:200]}")
            else:
                try:
                    body = resp.json() if kind == "json" else resp.text
                except ValueError as exc:
                    raise OpenDataError(f"{url}: not JSON ({resp.text[:120]!r})") from exc
            path.parent.mkdir(parents=True, exist_ok=True)
            write_atomic(path, json.dumps({"url": url, "fetched_at": time.time(), "body": body}))
            return body
        raise OpenDataError(f"{url}: gave up after retries ({last_error})")
