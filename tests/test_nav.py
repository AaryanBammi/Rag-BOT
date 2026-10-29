"""Latest-NAV lookup (app/nav.py) and its pipeline route, with a fake AMFI file."""

import asyncio
import re
from datetime import date

import pytest

from app.ask import Answerer
from app.classifier import Classification
from app.nav import NavFeed, NavUnavailable, parse_navall
from app.pipeline import Pipeline
from app.registry import load_registry
from ingest.manifest import DocumentRow, Manifest

REG = load_registry()
FEED_URL = REG.general_of_type("nav_feed")[0].url

NAVALL = """Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date

Open Ended Schemes(Equity Scheme - Mid Cap Fund)

HDFC Mutual Fund

118989;INF179K01XQ0;-;HDFC Mid Cap Fund;Direct Plan;Growth Option;216.123;27-Oct-2026
105758;INF179K01CR2;-;HDFC Mid Cap Fund;Regular Plan;Growth Option;198.456;27-Oct-2026
130503;INF179KA1RW5;-;HDFC Small Cap Fund;Direct Plan;Growth Option;150.714;24-Oct-2026
150737;INF179KC1DU7;-;HDFC Silver ETF Fund of Fund;Direct Plan;Growth Option;N.A.;27-Oct-2026
119018;INF179K01YV8;-;HDFC Large Cap Fund;Regular Plan;IDCW Option;55.10;27-Oct-2026
"""


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def counting(texts):
    """A fake download returning each item in turn (an Exception item is raised)."""
    calls = []

    def download(url):
        calls.append(url)
        item = texts[min(len(calls), len(texts)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return download, calls


def test_parse_skips_headers_and_unpriced_rows():
    rows = parse_navall(NAVALL)
    assert set(rows) == {"118989", "105758", "130503", "119018"}  # N.A. NAV skipped
    mid = rows["118989"]
    assert (mid.nav, mid.date, mid.is_direct_growth) == ("216.123", date(2026, 10, 27), True)
    assert not rows["105758"].is_direct_growth and not rows["119018"].is_direct_growth


def test_cache_reuses_file_until_ttl_then_refreshes():
    clock = Clock()
    download, calls = counting([NAVALL, NAVALL.replace("216.123", "217.000")])
    feed = NavFeed(FEED_URL, ttl=3600, download=download, clock=clock)
    assert feed.get("118989").nav == "216.123"
    clock.now = 3599
    assert feed.get("130503").nav == "150.714" and len(calls) == 1
    clock.now = 3600
    assert feed.get("118989").nav == "217.000" and len(calls) == 2


def test_failed_refresh_keeps_last_good_copy():
    clock = Clock()
    download, calls = counting([NAVALL, OSError("timeout"), "garbage"])
    feed = NavFeed(FEED_URL, ttl=10, download=download, clock=clock)
    assert feed.get("118989").nav == "216.123"
    clock.now = 20
    assert feed.get("118989").nav == "216.123"  # network error: stale copy, with its own date
    clock.now = 40
    assert feed.get("118989").nav == "216.123"  # file with no rows: also rejected
    assert len(calls) == 3


def test_no_copy_at_all_raises():
    feed = NavFeed(FEED_URL, download=counting([OSError("down")])[0])
    with pytest.raises(NavUnavailable):
        feed.get("118989")


def test_registry_codes_and_feed_host():
    codes = {s.id: s.amfi_code for s in REG.schemes}
    assert all(c and c.isdigit() for c in codes.values()) and len(set(codes.values())) == 6
    assert codes["hdfc-mid-cap"] == "118989"
    assert FEED_URL.startswith("https://portal.amfiindia.com/")


# --- pipeline route -------------------------------------------------------------------------

@pytest.fixture
def make_pipeline(tmp_path):
    manifest = Manifest(tmp_path / "m.json")
    for s in REG.all_sources():
        if s.fetch != "none":
            manifest.upsert(DocumentRow(s.id, s.url, s.type, s.scheme_id, s.title, "h",
                                        "2026-10-27T05:00:00+00:00", None, 1, "m", "2026-10-27T05:00:00+00:00"))

    class NoRetriever:
        def search(self, *a, **k):
            raise AssertionError("NAV questions must not reach retrieval")

    def build(scheme="hdfc-mid-cap", text=NAVALL):
        def classify(message, registry, history=()):
            return Classification("nav", scheme, False, False, message)

        async def aclassify(message, registry, history=()):
            return classify(message, registry, history)

        feed = NavFeed(FEED_URL, download=counting([text])[0])
        return Pipeline(Answerer(NoRetriever(), REG, manifest), classify, aclassify, nav=feed)

    return build


def test_nav_question_gets_the_figure_with_amfi_link_and_nav_date(make_pipeline):
    reply = make_pipeline().handle("What is the NAV of HDFC Mid Cap?")
    assert reply.kind == "nav_value" and reply.intent == "nav" and reply.scheme_id == "hdfc-mid-cap"
    body = reply.text.split("\n\nSource:")[0]
    assert "₹216.123" in body and "HDFC Mid Cap Fund – Direct Growth" in body and "27 Oct 2026" in body
    assert reply.url == FEED_URL and len(re.findall(r"https?://", reply.text)) == 1
    assert reply.text.endswith("Last updated from sources: 27 Oct 2026")
    assert "%" not in body  # a NAV, never a change or return


def test_holiday_nav_shows_its_own_date(make_pipeline):
    reply = make_pipeline(scheme="hdfc-small-cap").handle("NAV of HDFC Small Cap?")
    assert reply.kind == "nav_value" and reply.footer_date == "24 Oct 2026"


def test_async_path_matches(make_pipeline):
    reply = asyncio.run(make_pipeline().ahandle("What is the NAV of HDFC Mid Cap?"))
    assert reply.kind == "nav_value" and "₹216.123" in reply.text


@pytest.mark.parametrize("message", ["NAV of HDFC Mid Cap regular plan?", "Mid cap IDCW NAV?",
                                     "What is the dividend option NAV of HDFC Mid Cap?"])
def test_other_plans_get_the_scheme_page(make_pipeline, message):
    reply = make_pipeline().handle(message)
    assert reply.kind == "nav" and not re.search(r"\d", reply.text.split("\n\nSource:")[0])
    assert reply.url == REG.scheme("hdfc-mid-cap").source("scheme_page").url


def test_missing_row_or_feed_down_falls_back_to_scheme_page(make_pipeline):
    assert make_pipeline(scheme="hdfc-silver-etf-fof").handle("Silver FoF NAV?").kind == "nav"  # N.A. row
    assert make_pipeline().nav.get  # sanity: feed exists
    reply = Pipeline(make_pipeline().answerer, make_pipeline()._classify).handle("NAV of HDFC Mid Cap?")
    assert reply.kind == "nav" and "couldn't fetch" in reply.text  # default feed: offline in tests


def test_nav_without_scheme_asks_which(make_pipeline):
    assert make_pipeline(scheme=None).handle("What is the NAV?").kind == "clarification"


@pytest.mark.parametrize("message", ["What was the NAV of HDFC Mid Cap one year ago?", "Mid cap NAV in 2024?",
                                     "How much has the NAV of HDFC Mid Cap grown?", "NAV of mid cap since launch",
                                     "mid cap NAV change last month"])
def test_past_or_changing_nav_is_performance(make_pipeline, message):
    reply = make_pipeline().handle(message)
    assert reply.kind == "performance" and reply.intent == "performance"
    assert not re.search(r"\d", reply.text.split("\n\nSource:")[0].replace("2024", ""))


@pytest.mark.parametrize("message", ["What is the NAV of HDFC Mid Cap?", "current nav of hdfc midcap",
                                     "HDFC Mid Cap Fund NAV today", "latest NAV for Mid Cap please"])
def test_current_nav_wording_still_looks_up(make_pipeline, message):
    assert make_pipeline().handle(message).kind == "nav_value"
