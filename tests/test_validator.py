"""Validator checks with hand-written model outputs, no LLM (eval.md Phase 3, VAL/RAG cases)."""

import pytest

from app.generator import GeneratedAnswer
from app.retriever import RetrievedChunk
from app.validator import (
    check, count_sentences, has_advice, has_return_figures, strip_urls, ungrounded_numbers,
)

CHUNK_TEXT = ("HDFC Mid Cap Fund - Direct Plan. Total Expense Ratio: 0.71%. Exit Load: 1.00% if redeemed "
              "within 1 year from the date of allotment. Minimum application Rs. 1,000. Lock-in of three years.")


def _chunk(chunk_id="c1", text=CHUNK_TEXT):
    return RetrievedChunk(chunk_id=chunk_id, text=text, doc_id="d", scheme_id="hdfc-mid-cap", source_type="kim",
                          kind="text", url="https://files.hdfcfund.com/kim.pdf", title="KIM", as_of="2025-11-21",
                          fetched_at="2026-10-27T05:00:00+00:00", page=1, score=0.03, similarity=0.8)


@pytest.mark.parametrize("text,n", [
    ("The expense ratio is 0.71%.", 1),
    ("The minimum SIP is Rs. 100. It applies to daily, weekly and monthly SIPs.", 2),
    ("Funds e.g. ELSS have a lock-in. No. of years is 3. It is fixed.", 3),
    ("Exit load is 1.00% p.a. within 1 year", 1),
    ("First line\nSecond line\nThird line\nFourth line", 4),
    ("- one\n- two", 2),
    ("", 0),
])
def test_sentence_counter(text, n):
    assert count_sentences(text) == n


@pytest.mark.parametrize("text,expected", [
    ("See https://www.hdfcfund.com/x for details.", "See for details."),
    ("Visit hdfcfund.com to download it.", "Visit to download it."),
    ("The exit load is 1.00% (www.amfiindia.com).", "The exit load is 1.00%."),
    ("The expense ratio is 0.71%.", "The expense ratio is 0.71%."),
])
def test_url_stripper(text, expected):
    assert strip_urls(text) == expected


def test_numeric_grounding():
    assert ungrounded_numbers("The exit load is 1% within 1 year.", CHUNK_TEXT) == []
    assert ungrounded_numbers("The minimum application is Rs. 1000.", CHUNK_TEXT) == []
    assert ungrounded_numbers("The lock-in is 3 years.", CHUNK_TEXT) == []  # "three" in the chunk
    assert ungrounded_numbers("The expense ratio is 0.76%.", CHUNK_TEXT) == ["0.76"]
    assert ungrounded_numbers("As of 30 Sep 2026 it is 0.71%.", CHUNK_TEXT) == ["30", "2026"]


@pytest.mark.parametrize("text", [
    "The fund's 3-year return is 25.4%.",
    "It delivered a CAGR of 18%.",
    "Returns since inception are strong.",
    "1Y: 12%, 3Y: 20%",
    "It returned 14.2% last year.",
])
def test_return_figures_rejected(text):
    assert has_return_figures(text)


@pytest.mark.parametrize("text", [
    "The exit load is 1.00% if redeemed within 1 year.",
    "The lock-in period is 3 years from the date of allotment.",
    "The benchmark is NIFTY Midcap 150 TRI.",
])
def test_facts_are_not_return_figures(text):
    assert not has_return_figures(text)


@pytest.mark.parametrize("text", [
    "You should invest in this fund for the long term.",
    "I recommend the Direct Plan.",
    "It is a good investment for retirement.",
    "This is the better option for you.",
    "You can consider a SIP.",
])
def test_advice_rejected(text):
    assert has_advice(text)


@pytest.mark.parametrize("text", [
    "Investors should note that the lock-in is 3 years.",
    "You can redeem units after the 3-year lock-in.",
    "You need to log in with your PAN to download the statement.",
    "You should log in to the HDFC MF website and choose Request Statement.",
])
def test_quoted_rules_and_how_to_steps_allowed(text):
    assert not has_advice(text)


def test_check_routes_failures():
    chunks = [_chunk()]
    ok = check(GeneratedAnswer(True, "The exit load is 1.00% within 1 year (hdfcfund.com).", "c1"), chunks)
    assert ok.ok and "hdfcfund" not in ok.answer and ok.cited.chunk_id == "c1"

    for gen in (GeneratedAnswer(False, "", ""), GeneratedAnswer(True, "  ", "c1"), GeneratedAnswer(True, "1%", "zz")):
        v = check(gen, chunks)
        assert not v.ok and not v.retry and v.fallback == "not_found"

    v = check(GeneratedAnswer(True, "The 3-year return is 0.71%.", "c1"), chunks)
    assert v.retry and v.fallback == "performance"
    v = check(GeneratedAnswer(True, "You should invest; the exit load is 1%.", "c1"), chunks)
    assert v.retry and v.fallback == "advisory"
    v = check(GeneratedAnswer(True, "One. Two. Three.", "c1"), chunks, max_sentences=2)
    assert v.retry and v.fallback == "not_found"
    v = check(GeneratedAnswer(True, "The expense ratio is 0.76%.", "c1"), chunks)
    assert not v.retry and v.fallback == "not_found"
