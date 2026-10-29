"""Checks every auto-fetched registry URL is live and stays on an allowed host.

Run with: pytest --run-network

Sources with fetch `none` or `manual` are on www.hdfcfund.com, which returns 403 to automated
clients; they were verified in a browser and are not requested here.
"""

import httpx
import pytest

from app.config import is_allowed_url
from app.registry import load_registry

pytestmark = pytest.mark.network

AUTO_SOURCES = [s for s in load_registry().all_sources() if s.fetch == "auto"]


@pytest.mark.parametrize("source", AUTO_SOURCES, ids=lambda s: s.id)
def test_source_reachable_and_allowed(source):
    with httpx.Client(follow_redirects=True, timeout=60, headers={"User-Agent": "Mozilla/5.0"}) as client:
        with client.stream("GET", source.url) as response:
            assert response.status_code == 200, f"{source.id}: HTTP {response.status_code}"
            assert is_allowed_url(str(response.url)), f"{source.id}: redirected off allow-list to {response.url}"
            first = next(response.iter_bytes(), b"")
    if ".pdf" in source.url.lower():
        assert first.startswith(b"%PDF"), f"{source.id}: expected a PDF"


def test_nav_feed_has_every_scheme_as_direct_growth():
    from app.nav import NavFeed

    reg = load_registry()
    feed = NavFeed.from_registry(reg)
    for scheme in reg.schemes:
        row = feed.get(scheme.amfi_code)
        assert row is not None, f"{scheme.id}: AMFI code {scheme.amfi_code} not in the NAV file"
        assert row.is_direct_growth, f"{scheme.id}: code {scheme.amfi_code} is {row.plan} / {row.option}"
