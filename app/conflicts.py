"""Conflict rule (implementation_plan.md Phase 2): prefer the most recent source for changing figures.

Applied after retrieval and before generation. When the question asks about a figure that
changes over time (expense ratio, AUM, NAV, portfolio turnover), every scheme chunk that states
that figure is compared by `as_of`, and the older ones are dropped. The model then only sees the
latest value, so it can't cite a stale one (e.g. the Nov 2025 KIM's turnover ratio next to the
Sep 2026 fund facts).

Only scheme-tagged, dated chunks take part: general documents (SAI, AMFI/SEBI pages) describe
rules and limits rather than a scheme's current figure, and an undated chunk can't be ordered.
Static terms (exit load, lock-in, benchmark, minimum SIP) aren't changing figures, so questions
about them are left alone.
"""

from __future__ import annotations

import re

from app.retriever import RetrievedChunk

CHANGING_FIGURES: dict[str, re.Pattern[str]] = {
    "expense_ratio": re.compile(r"expense\s+ratio|\bTER\b", re.I),
    "aum": re.compile(r"\bAUM\b|assets\s+under\s+management", re.I),
    "nav": re.compile(r"\bNAV\b|net\s+asset\s+value", re.I),
    "portfolio_turnover": re.compile(r"turnover", re.I),
}
_NUMBER = re.compile(r"\d")
# A chunk "states" a figure when a number sits near its label (before it, as in "₹101,856.06Cr.\nAUM",
# or after it, as in "Total Expense Ratio ... 0.76%"). A definition with no number doesn't count.
_BEFORE, _AFTER = 40, 120


def question_topics(question: str) -> list[str]:
    return [name for name, pattern in CHANGING_FIGURES.items() if pattern.search(question)]


def states_figure(text: str, topic: str) -> bool:
    for m in CHANGING_FIGURES[topic].finditer(text):
        window = text[max(0, m.start() - _BEFORE): m.end() + _AFTER]
        if _NUMBER.search(window):
            return True
    return False


def prefer_latest(question: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Drop scheme chunks that state an asked-about changing figure but are older than the newest one.

    Keeps the retriever's order for everything that remains.
    """
    stale: set[str] = set()
    for topic in question_topics(question):
        stating = [c for c in chunks if c.scheme_id and c.as_of and states_figure(c.text, topic)]
        if len(stating) < 2:
            continue
        newest = max(c.as_of for c in stating)
        stale.update(c.chunk_id for c in stating if c.as_of < newest)
    return [c for c in chunks if c.chunk_id not in stale]
