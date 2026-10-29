"""Finds a document's own "as of" date (architecture.md §3.2, edge cases PRS-07 – PRS-09).

Rules per source type:
  factsheet, fund_facts  most frequent "as on / as of / as at" date (a factsheet repeats the
                         month-end date many times; one-off dates such as NAV dates lose)
  sid, kim, sai          "dated <date>" near the start of the document, else "effective from"
  anything else          the first "as on / as of / effective from / updated on" date, if any

A date in the future or before 1990 is treated as misparsed and ignored, so the caller gets
None (and the footer falls back to `fetched_at`) rather than a wrong date.
"""

from __future__ import annotations

import calendar
import re
from collections import Counter
from datetime import date, timedelta

_MONTHS = {
    name.lower(): i
    for i in range(1, 13)
    for name in (calendar.month_name[i], calendar.month_abbr[i])
}
_MONTHS["sept"] = 9
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))

_DAY = r"(?P<d>\d{1,2})(?:st|nd|rd|th)?"
_YEAR = r"(?P<y>\d{4}|\d{2})"

# Ordered most specific first. Each pattern has named groups d (optional), m, y.
_DATE_PATTERNS = [
    # September 30, 2026 | Sept 30th 2026
    re.compile(rf"(?P<m>{_MONTH_RE})\.?\s+{_DAY},?\s+(?P<y>\d{{4}})", re.I),
    # 30 September 2026 | 30th September, 2026 | 30-Sep-26 | 30-Sep-2026
    re.compile(rf"{_DAY}[\s\-]+(?P<m>{_MONTH_RE})\.?[\s,\-]+{_YEAR}\b", re.I),
    # 30/09/2026 | 30-09-2026 | 30.09.2026
    re.compile(r"(?P<d>\d{1,2})[/\-.](?P<mn>\d{1,2})[/\-.](?P<y>\d{4})\b"),
    # September 2026 (month only -> last day of the month)
    re.compile(rf"(?P<m>{_MONTH_RE})\.?,?\s+(?P<y>\d{{4}})\b", re.I),
]

_AS_ON = r"(?:as\s+(?:on|of|at)|as\s+on\s+date)"
_EFFECTIVE = r"(?:effective\s+(?:from|date|on)?|w\.?e\.?f\.?)"
_DATED = r"dated"
_UPDATED = r"(?:last\s+)?updated(?:\s+on)?"

EARLIEST = date(1990, 1, 1)


def _build(y: str, m: int, d: str | None) -> date | None:
    year = int(y)
    if year < 100:
        year += 2000
    try:
        if d is None:
            return date(year, m, calendar.monthrange(year, m)[1])
        return date(year, m, int(d))
    except ValueError:
        return None


def parse_date(text: str) -> date | None:
    """Parse the first date in `text` (any supported format), or None."""
    for pattern in _DATE_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        g = match.groupdict()
        month = int(g["mn"]) if g.get("mn") else _MONTHS[g["m"].lower().rstrip(".")]
        if not 1 <= month <= 12:
            continue
        return _build(g["y"], month, g.get("d"))
    return None


def _dates_after(keyword: str, text: str) -> list[date]:
    """Every date that directly follows `keyword` (allowing ':' '(' '-' and up to 3 words)."""
    found = []
    for match in re.finditer(rf"\b{keyword}\b[\s:(\-]*", text, re.I):
        window = text[match.end() : match.end() + 40]
        # The date must start the window, possibly after a couple of filler words ("the", "date").
        window = re.sub(r"^(?:(?:the|date|on|from)\s+){0,3}", "", window, flags=re.I)
        for pattern in _DATE_PATTERNS:
            m = pattern.match(window)
            if m:
                parsed = parse_date(m.group(0))
                if parsed:
                    found.append(parsed)
                break
    return found


def _plausible(d: date, today: date) -> bool:
    return EARLIEST <= d <= today + timedelta(days=1)


def extract_as_of(text: str, source_type: str, today: date | None = None) -> date | None:
    """The document's own date for `source_type`, or None if none is found or it looks wrong."""
    today = today or date.today()

    def keep(dates: list[date]) -> list[date]:
        return [d for d in dates if _plausible(d, today)]

    if source_type in {"factsheet", "fund_facts"}:
        dates = keep(_dates_after(_AS_ON, text))
        if not dates:
            return None
        counts = Counter(dates)
        top = max(counts.values())
        # Tie-break towards the most recent date.
        return max(d for d, c in counts.items() if c == top)

    if source_type in {"sid", "kim", "sai"}:
        head = text[:3000]
        dates = keep(_dates_after(_DATED, head)) or keep(_dates_after(_EFFECTIVE, head))
        return dates[0] if dates else None

    for keyword in (_AS_ON, _EFFECTIVE, _UPDATED):
        dates = keep(_dates_after(keyword, text))
        if dates:
            return dates[0]
    return None
