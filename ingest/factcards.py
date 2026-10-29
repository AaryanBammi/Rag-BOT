"""Per-scheme "fact cards" from the combined factsheet (Docs/data_research.md §4.4).

Factsheet pages are dense blocks of figures, and in search they lose to SID/SAI prose that
discusses the same topics in general terms (e.g. expense-ratio rules vs. the actual expense
ratio). A fact card is one short chunk per scheme listing the key monthly figures on labelled
lines, so both BM25 and embeddings match it directly.

Cards are extracted, never generated: each value is copied verbatim from the factsheet text. A
field is left out when its pattern doesn't match, or when the text is missing its value (the
Silver ETF FoF exit-load percentage is drawn as a graphic and reads "an Exit Load of is payable").
"""

from __future__ import annotations

import re

from ingest.chunk import Chunk, tag_pages
from ingest.parse import ParsedDoc

MAX_FIELD_LINES = 8

# A line that starts a new factsheet section (ends a field's value).
_SECTION_START = re.compile(r"^(#|\.\.\.\.Contd|[A-Z][A-Z0-9 &'/().,:-]{3,}\$*$)")
# A value whose number didn't survive extraction ("an Exit Load of is payable").
_MISSING_VALUE = re.compile(r"\bof\s+(is|are|shall)\b", re.I)


def _expense_ratio(text: str) -> str | None:
    m = re.search(
        r"EXPENSE RATIO\s*\n?\s*\(As On ([^)]+)\)[\s\S]{0,400}?(Regular:\s*[\d.]+%\s*Direct:\s*[\d.]+%)", text
    )
    return f"(as on {m.group(1).strip()}) {' '.join(m.group(2).split())}" if m else None


def _block_after(heading: str, text: str, max_lines: int = MAX_FIELD_LINES) -> str | None:
    """The lines after `heading` up to the next section heading."""
    m = re.search(rf"^{heading}\$*\s*$", text, re.M)
    if not m:
        return None
    out = []
    for line in text[m.end():].lstrip("\n").splitlines()[:max_lines]:
        if out and _SECTION_START.match(line.strip()):
            break
        out.append(line.strip())
    value = " ".join(" ".join(out).split())
    return value or None


FIELDS = {
    "Expense ratio": _expense_ratio,
    "Benchmark": lambda t: _block_after(r"#BENCHMARK INDEX", t, max_lines=2),
    "Exit load": lambda t: _block_after(r"EXIT LOAD", t),
    "Lock-in period": lambda t: _block_after(r"LOCK-IN PERIOD", t, max_lines=2),
}


def build_fact_cards(
    doc: ParsedDoc,
    title: str,
    name_index: dict[str, str],
    scheme_names: dict[str, str],
    start_index: int,
) -> list[Chunk]:
    """One fact card per registry scheme found in the multi-scheme factsheet."""
    pages_by_scheme: dict[str, list] = {}
    for page, tag in zip(doc.pages, tag_pages(doc.pages, name_index)):
        if tag:
            pages_by_scheme.setdefault(tag, []).append(page)

    cards = []
    for scheme_id, pages in pages_by_scheme.items():
        text = "\n".join(p.text for p in pages)
        name = scheme_names[scheme_id]
        lines = []
        for label, extract in FIELDS.items():
            value = extract(text)
            if value and not _MISSING_VALUE.search(value):
                lines.append(f"{name} - {label}: {value}")
        if lines:
            header = f"[Fact card | {title} | {name} | page {pages[0].number}]"
            cards.append(
                Chunk(
                    index=start_index + len(cards),
                    text="\n".join([header, *lines]),
                    page=pages[0].number,
                    scheme_id=scheme_id,
                    kind="fact_card",
                )
            )
    return cards
