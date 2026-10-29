"""Every template has exactly one link of the right type and a footer (architecture.md §4.6)."""

import re

import pytest

from app.registry import load_registry
from app.templates import Templates
from app.validator import count_sentences
from ingest.manifest import DocumentRow, Manifest

REG = load_registry()


@pytest.fixture
def templates(tmp_path):
    m = Manifest(tmp_path / "manifest.json")
    for s in REG.all_sources():
        if s.type not in {"scheme_page", "amc_service"}:
            m.upsert(DocumentRow(s.id, s.url, s.type, s.scheme_id, s.title, "h", "2026-10-27T05:00:00+00:00",
                                 "2026-09-30" if s.type in {"fund_facts", "factsheet"} else None, 1, "m",
                                 "2026-10-27T05:00:00+00:00"))
    return Templates(REG, m)


def _type_of(url):
    return next(s.type for s in REG.all_sources() if s.url == url)


CASES = [
    ("advisory", lambda t: t.advisory(), {"sebi_education", "amfi_education"}),
    ("performance", lambda t: t.performance("hdfc-mid-cap"), {"fund_facts"}),
    ("performance (silver: no fund facts)", lambda t: t.performance("hdfc-silver-etf-fof"), {"factsheet"}),
    ("performance (no scheme)", lambda t: t.performance(None), {"factsheet"}),
    ("nav", lambda t: t.nav("hdfc-small-cap"), {"scheme_page"}),
    ("multi_scheme", lambda t: t.multi_scheme(), {"amfi_education"}),
    ("clarification", lambda t: t.clarification(), {"amfi_education"}),
    ("greeting", lambda t: t.greeting(), {"amfi_education"}),
    ("out_of_scope", lambda t: t.out_of_scope(), {"amfi_education"}),
    ("not_found", lambda t: t.not_found("hdfc-large-cap"), {"scheme_page"}),
    ("not_found (no scheme)", lambda t: t.not_found(None), {"amfi_education"}),
    ("pii", lambda t: t.pii(), {"investor_safety"}),
]


@pytest.mark.parametrize("name,make,types", CASES, ids=[c[0] for c in CASES])
def test_one_link_of_right_type_and_footer(templates, name, make, types):
    reply = make(templates)
    assert len(re.findall(r"https?://", reply.text)) == 1
    assert _type_of(reply.url) in types
    assert re.search(r"\nLast updated from sources: \d{2} \w{3} \d{4}$", reply.text)
    body = reply.text.split("\n\nSource:")[0]
    assert count_sentences(body) <= 3
    assert not re.search(r"\d", body.replace("six", "")) or name.startswith("multi")  # no figures


def test_performance_footer_uses_factsheet_date(templates):
    assert templates.performance("hdfc-mid-cap").footer_date == "30 Sep 2026"


def test_lists_all_six_schemes(templates):
    text = templates.clarification().text
    for s in REG.schemes:
        assert s.name.split(" – ")[0] in text


def test_service_unavailable_has_no_link():
    reply = Templates.service_unavailable()
    assert reply.url is None and "http" not in reply.text
