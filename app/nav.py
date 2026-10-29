"""Latest NAV from AMFI's daily NAV file (decision D-06, revised 2026-10-28).

    feed = NavFeed.from_registry(registry)
    row = feed.get("118989")   # HDFC Mid Cap Fund - Direct Plan - Growth Option

NAV changes every business day, so it is looked up live instead of being ingested: the file is
downloaded on demand and kept in memory for `ttl` seconds. If a refresh fails, the last good copy
is served (its rows carry their own NAV date, so the reply never looks fresher than it is); with
no copy at all, `NavUnavailable` is raised and the pipeline falls back to the "check the scheme
page" template. The value is returned exactly as AMFI publishes it, never recomputed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

import httpx

from app.config import is_allowed_url
from app.registry import Registry

TTL_SECONDS = 3600.0
TIMEOUT_SECONDS = 15.0
USER_AGENT = "Mozilla/5.0 (compatible; mf-faq-assistant/1.0)"


class NavUnavailable(Exception):
    pass


@dataclass(frozen=True)
class NavRow:
    code: str
    name: str
    plan: str
    option: str
    nav: str  # as published, e.g. "216.123"
    date: date

    @property
    def is_direct_growth(self) -> bool:
        return "direct" in self.plan.lower() and "growth" in self.option.lower()


def parse_navall(text: str) -> dict[str, NavRow]:
    """Rows of NAVAll.txt by scheme code. Header, AMC/category lines and rows without a numeric NAV
    or a valid date are skipped."""
    rows: dict[str, NavRow] = {}
    for line in text.splitlines():
        parts = [p.strip() for p in line.split(";")]
        if len(parts) != 8 or not parts[0].isdigit():
            continue
        code, _, _, name, plan, option, nav, day = parts
        try:
            float(nav)
            when = datetime.strptime(day, "%d-%b-%Y").date()
        except ValueError:
            continue
        rows[code] = NavRow(code, name, plan, option, nav, when)
    return rows


def _download(url: str) -> str:
    with httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as client:
        response = client.get(url)
        if not is_allowed_url(str(response.url)):
            raise NavUnavailable(f"NAV feed redirected to a host that is not allowed: {response.url}")
        response.raise_for_status()
        return response.text


class NavFeed:
    def __init__(self, url: str, ttl: float = TTL_SECONDS, download: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.url, self.ttl, self._download, self._clock = url, ttl, download or _download, clock
        self._rows: dict[str, NavRow] | None = None
        self._loaded_at = float("-inf")
        self._lock = threading.Lock()  # one download at a time; waiting callers reuse its result

    @classmethod
    def from_registry(cls, registry: Registry, **kwargs) -> "NavFeed":
        return cls(registry.general_of_type("nav_feed")[0].url, **kwargs)

    def _current(self) -> dict[str, NavRow]:
        with self._lock:
            if self._rows is not None and self._clock() - self._loaded_at < self.ttl:
                return self._rows
            try:
                rows = parse_navall(self._download(self.url))
                if not rows:
                    raise NavUnavailable("NAV feed had no rows")
                self._rows, self._loaded_at = rows, self._clock()
            except Exception as e:  # network, HTTP or parse failure: keep serving the last good copy
                if self._rows is None:
                    raise NavUnavailable(str(e)) from e
            return self._rows

    def get(self, code: str) -> NavRow | None:
        return self._current().get(code)

    async def aget(self, code: str) -> NavRow | None:
        return await asyncio.to_thread(self.get, code)
