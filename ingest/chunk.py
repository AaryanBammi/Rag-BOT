"""Splits parsed documents into chunks (architecture.md §3.2, Docs/data_research.md §1).

- Chunks are built from whole lines, so a table row is never split.
- Target ~200 words with ~30 words of overlap, and a hard cap of `max_tokens` counted with the
  embedding model's own tokenizer (the corpus averages ~1.5 tokens per word, and bge models
  silently drop input past 512 tokens). A single line longer than the cap is split by words.
- A chunk breaks early at a heading line once it is reasonably full, so sections such as
  "EXIT LOAD" or "BENCHMARK" tend to start a chunk.
- Each chunk starts with a context line naming the document and scheme, so scheme names are
  part of every embedding even in long SIDs that rarely repeat them.

Multi-scheme documents (the combined factsheet) are split by page: a page belongs to a scheme
when one of its first lines is exactly that scheme's name or alias. Pages that match no
registry scheme (other HDFC schemes, shared annexures) are dropped, never guessed.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from ingest.parse import Page, ParsedDoc

# Bump when chunking or fact-card output changes, so the next ingest re-chunks every document
# even though its content hash is unchanged.
CHUNKER_VERSION = "3"

TARGET_WORDS = 200
OVERLAP_WORDS = 30
MAX_TOKENS = 480  # bge-small reads 512 tokens including special tokens; keep a margin
MIN_FILL_FOR_HEADING_BREAK = 0.6
HEADING_SCAN_LINES = 6

TokenCounter = Callable[[str], int]


def approx_tokens(text: str) -> int:
    """Conservative token estimate (~3 characters per token) when no tokenizer is available."""
    return math.ceil(len(text) / 3)


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    page: int | None
    scheme_id: str | None
    kind: str = "text"  # "text" or "fact_card"


def _norm_name(text: str) -> str:
    text = text.lower().replace("&", " and ").replace("–", "-").replace("—", "-")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def scheme_name_index(names_by_scheme: dict[str, list[str]]) -> dict[str, str]:
    """Normalised name -> scheme_id. Raises if two schemes share a name."""
    index: dict[str, str] = {}
    for scheme_id, names in names_by_scheme.items():
        for name in names:
            key = _norm_name(name)
            if key in index and index[key] != scheme_id:
                raise ValueError(f"name {name!r} is used by both {index[key]} and {scheme_id}")
            index[key] = scheme_id
    return index


def tag_pages(pages: tuple[Page, ...], name_index: dict[str, str]) -> list[str | None]:
    """scheme_id for each page whose first lines contain an exact scheme name, else None."""
    tags = []
    for page in pages:
        tag = None
        for line in page.text.splitlines()[:HEADING_SCAN_LINES]:
            tag = name_index.get(_norm_name(line))
            if tag:
                break
        tags.append(tag)
    return tags


# Numbered or roman-numeral section titles, as used in KIMs and SIDs:
# "11. Minimum Application Amount / Number of Units", "XV. Segregated portfolio".
_NUMBERED_HEADING = re.compile(r"^(?:\d{1,2}|[IVXL]{1,5})\.\s+[A-Z][^.:;]{2,80}$")


def _is_heading(line: str) -> bool:
    letters = re.sub(r"[^A-Za-z]", "", line)
    if 3 <= len(letters) and len(line.split()) <= 8 and letters.isupper():
        return True
    return len(line.split()) <= 10 and bool(_NUMBERED_HEADING.match(line))


def _split_long_line(line: str, budget: int, count: TokenCounter) -> list[str]:
    """Split a line that alone exceeds `budget` tokens into word groups that fit."""
    pieces, current = [], []
    for word in line.split():
        if current and count(" ".join([*current, word])) > budget:
            pieces.append(" ".join(current))
            current = []
        current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def _chunk_lines(
    lines: list[tuple[str, int | None]], budget: int, count: TokenCounter
) -> list[tuple[str, int | None]]:
    """Group (line, page) pairs into (text, first_page) chunks by word and token budget."""
    sized: list[tuple[str, int | None, int, int]] = []  # line, page, words, tokens
    for line, page in lines:
        t = count(line) + 1  # +1 for the newline
        parts = _split_long_line(line, budget // 2, count) if t > budget else [line]
        for part in parts:
            sized.append((part, page, len(part.split()), count(part) + 1))

    chunks: list[tuple[str, int | None]] = []
    current: list[tuple[str, int | None, int, int]] = []

    def emit() -> None:
        chunks.append(("\n".join(item[0] for item in current), current[0][1]))

    for item in sized:
        words = sum(i[2] for i in current)
        tokens = sum(i[3] for i in current)
        full = words >= TARGET_WORDS * MIN_FILL_FOR_HEADING_BREAK
        if current and (
            words + item[2] > TARGET_WORDS
            or tokens + item[3] > budget
            or (full and _is_heading(item[0]))
        ):
            emit()
            # Carry the last lines forward as overlap, within both budgets.
            carry: list[tuple[str, int | None, int, int]] = []
            for prev in reversed(current):
                if sum(i[2] for i in carry) >= OVERLAP_WORDS:
                    break
                carry.insert(0, prev)
            if len(carry) == len(current) or sum(i[3] for i in carry) + item[3] > budget:
                carry = []
            current = carry
        current.append(item)
    if current:
        emit()
    return chunks


def chunk_document(
    doc: ParsedDoc,
    title: str,
    scheme_id: str | None,
    scheme_names: dict[str, str],
    multi_scheme_index: dict[str, str] | None = None,
    count_tokens: TokenCounter = approx_tokens,
    max_tokens: int = MAX_TOKENS,
) -> list[Chunk]:
    """Chunk `doc`. For multi-scheme documents pass `multi_scheme_index` (see scheme_name_index).

    `scheme_names` maps scheme_id -> display name, used in each chunk's context line.
    `count_tokens` / `max_tokens` come from the embedder so chunks fit its input limit.
    """
    if multi_scheme_index is not None:
        tags = tag_pages(doc.pages, multi_scheme_index)
        sections: list[tuple[str | None, list[Page]]] = []
        for page, tag in zip(doc.pages, tags):
            if tag is None:
                continue
            if sections and sections[-1][0] == tag:
                sections[-1][1].append(page)
            else:
                sections.append((tag, [page]))
    else:
        sections = [(scheme_id, list(doc.pages))]

    out: list[Chunk] = []
    for section_scheme, pages in sections:
        lines = [(line, p.number) for p in pages for line in p.text.splitlines()]
        context = title if not section_scheme else f"{title} | {scheme_names[section_scheme]}"
        header_budget = count_tokens(f"[{context} | page 9999]") + 1
        for text, page in _chunk_lines(lines, max_tokens - header_budget, count_tokens):
            header = f"[{context}" + (f" | page {page}]" if page else "]")
            out.append(Chunk(index=len(out), text=f"{header}\n{text}", page=page, scheme_id=section_scheme))
    return out
