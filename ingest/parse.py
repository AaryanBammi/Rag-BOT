"""PDF and HTML text extraction (architecture.md §3.2).

PDFs use pdfplumber's text-flow mode, which follows the PDF's own reading order. HDFC's
two-column factsheet pages come out mixed in pdfplumber's default mode (the expense ratio block
interleaves with the holdings table) and in reading order with text flow.

Failures raise ParseError so the pipeline keeps the previous version of the document
(edge cases ING-07, ING-08, PRS-01, PRS-02).
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

import pdfplumber
import trafilatura
from bs4 import BeautifulSoup

# Below this many characters of text a document is treated as unparseable: a scanned PDF with
# no text layer, or an HTML shell whose content is rendered by JavaScript.
MIN_TEXT_CHARS = 200


class ParseError(Exception):
    pass


@dataclass(frozen=True)
class Page:
    number: int | None  # 1-based page number for PDFs, None for HTML
    text: str


@dataclass(frozen=True)
class ParsedDoc:
    kind: str  # "pdf" or "html"
    pages: tuple[Page, ...]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)


def detect_kind(raw: bytes) -> str:
    """'pdf' or 'html' from the file's bytes, ignoring the server's Content-Type (ING-08)."""
    head = raw[:1024].lstrip()
    if head.startswith(b"%PDF"):
        return "pdf"
    lowered = head.lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<?xml", b"<!--", b"<meta")):
        return "html"
    raise ParseError("unrecognised file type (neither PDF nor HTML)")


def _normalize(text: str) -> str:
    lines = (re.sub(r"[ \t ]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def parse_pdf(raw: bytes) -> ParsedDoc:
    try:
        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            pages = tuple(
                Page(number=i, text=_normalize(page.extract_text(use_text_flow=True) or ""))
                for i, page in enumerate(pdf.pages, start=1)
            )
    except Exception as e:  # pdfminer raises many types for corrupt/encrypted files
        raise ParseError(f"could not read PDF (corrupt or password-protected?): {type(e).__name__}") from None
    doc = ParsedDoc(kind="pdf", pages=tuple(p for p in pages if p.text))
    if len(doc.text) < MIN_TEXT_CHARS:
        raise ParseError("PDF has no extractable text (scanned image?)")
    return doc


def parse_html(raw: bytes) -> ParsedDoc:
    html = raw.decode("utf-8", errors="replace")
    text = trafilatura.extract(html, include_tables=True, include_comments=False, favor_recall=True) or ""
    if len(text) < MIN_TEXT_CHARS:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form"]):
            tag.decompose()
        text = soup.get_text("\n")
    text = _normalize(text)
    if len(text) < MIN_TEXT_CHARS:
        raise ParseError("page has too little text (content may be rendered by JavaScript)")
    return ParsedDoc(kind="html", pages=(Page(number=None, text=text),))


def parse(raw: bytes) -> ParsedDoc:
    kind = detect_kind(raw)
    return parse_pdf(raw) if kind == "pdf" else parse_html(raw)
