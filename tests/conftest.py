"""Shared pytest setup.

Markers:
  network  hits public websites (source URL checks). Run with --run-network.
  live     calls Groq / Voyage with real API keys. Run with --run-live.
Both are skipped by default so `pytest` stays fast, offline, and free.
"""

import pytest


def pytest_addoption(parser):
    parser.addoption("--run-network", action="store_true", help="run tests that hit public websites")
    parser.addoption("--run-live", action="store_true", help="run tests that call Groq/Voyage APIs")


def pytest_configure(config):
    config.addinivalue_line("markers", "network: hits public websites")
    config.addinivalue_line("markers", "live: calls paid APIs with real keys")


def pytest_collection_modifyitems(config, items):
    skips = {
        "network": (config.getoption("--run-network"), "needs --run-network"),
        "live": (config.getoption("--run-live"), "needs --run-live"),
    }
    for item in items:
        for marker, (enabled, reason) in skips.items():
            if marker in item.keywords and not enabled:
                item.add_marker(pytest.mark.skip(reason=reason))


@pytest.fixture(autouse=True)
def offline_nav(request, monkeypatch):
    """Offline tests never download AMFI's NAV file: a pipeline built without a NavFeed sees the
    feed as unavailable (the "check the scheme page" fallback). Tests that need a NAV inject a fake."""
    if "network" in request.keywords or "live" in request.keywords:
        return

    def no_network(url):
        raise OSError("network disabled in offline tests")

    monkeypatch.setattr("app.nav._download", no_network)
