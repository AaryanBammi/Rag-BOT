"""Gets the raw bytes for a registry source (architecture.md §3.2, edge cases ING-01 – ING-03, ING-06).

- `auto` sources are downloaded with retries; the allow-list is checked on the final URL after
  redirects, and 4xx responses fail immediately with a clear message.
- `manual` sources are read from data/raw/manual/<source id>.<html|htm|pdf>, saved there by a
  person from a browser because the host blocks automated downloads. Their fetch time is the
  file's modification time (when it was saved).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.config import is_allowed_url
from app.registry import GROWW_HOSTS, Source

USER_AGENT = "Mozilla/5.0 (compatible; MF-FAQ-Assistant/0.1; facts-only RAG ingestion)"
TIMEOUT_SECONDS = 60
ATTEMPTS = 3
BACKOFF_SECONDS = 2.0
MANUAL_EXTENSIONS = (".html", ".htm", ".pdf")


class FetchError(Exception):
    pass


class ManualFileMissing(FetchError):
    pass


@dataclass(frozen=True)
class Fetched:
    raw: bytes
    final_url: str
    fetched_at: str  # ISO 8601 UTC


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def manual_path(source: Source, raw_dir: Path) -> Path | None:
    for ext in MANUAL_EXTENSIONS:
        candidate = raw_dir / "manual" / f"{source.id}{ext}"
        if candidate.exists():
            return candidate
    return None


def fetch_manual(source: Source, raw_dir: Path) -> Fetched:
    path = manual_path(source, raw_dir)
    if path is None:
        raise ManualFileMissing(
            f"save {source.url} from a browser as {raw_dir / 'manual' / (source.id + '.html')}"
        )
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).replace(microsecond=0)
    return Fetched(raw=path.read_bytes(), final_url=source.url, fetched_at=mtime.isoformat())


def fetch_auto(source: Source, client: httpx.Client, sleep=time.sleep) -> Fetched:
    last_error = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            response = client.get(source.url)
        except httpx.TransportError as e:  # timeouts, DNS, connection resets
            last_error = f"{type(e).__name__}: {e}"
        else:
            final_url = str(response.url)
            if not is_allowed_url(final_url):
                raise FetchError(f"redirected to a host that is not allowed: {final_url}")
            if response.status_code == 200:
                return Fetched(raw=response.content, final_url=final_url, fetched_at=_now())
            if response.status_code < 500 and response.status_code != 429:
                hint = " (site blocks automated downloads; use fetch: manual)" if response.status_code == 403 else ""
                raise FetchError(f"HTTP {response.status_code}{hint}")
            last_error = f"HTTP {response.status_code}"
        if attempt < ATTEMPTS:
            sleep(BACKOFF_SECONDS * 2 ** (attempt - 1))
    raise FetchError(f"failed after {ATTEMPTS} attempts: {last_error}")


def fetch_reference(url: str, client: httpx.Client) -> Fetched:
    """Download a Groww page as a product-context snapshot (raw only; never parsed or cited).

    Groww is not an allowed *source*, so this is separate from fetch_auto and only accepts
    Groww hosts (before and after redirects).
    """
    if urlsplit(url).hostname not in GROWW_HOSTS:
        raise FetchError(f"not a Groww reference URL: {url}")
    try:
        response = client.get(url)
    except httpx.TransportError as e:
        raise FetchError(f"{type(e).__name__}: {e}") from None
    if urlsplit(str(response.url)).hostname not in GROWW_HOSTS:
        raise FetchError(f"redirected off Groww: {response.url}")
    if response.status_code != 200:
        raise FetchError(f"HTTP {response.status_code}")
    return Fetched(raw=response.content, final_url=str(response.url), fetched_at=_now())


def http_client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT})
