"""Response validator and formatter (architecture.md §4.5).

The citation link and the footer date always come from stored metadata, never from the model:
    <answer>

    Source: <url>
    Last updated from sources: <date>

The checks below run on the generator's answer before anything reaches the user. Each returns a
`Verdict`; the answer path (app/ask.py) decides whether to regenerate once or fall back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.generator import GeneratedAnswer
    from app.retriever import RetrievedChunk

IST = timezone(timedelta(hours=5, minutes=30))
DATE_FORMAT = "%d %b %Y"  # 30 Sep 2026


@dataclass(frozen=True)
class Reply:
    kind: str  # "answer", "not_found", or a template name (see app/templates.py)
    text: str  # the full formatted message shown to the user
    url: str | None  # None only for "service_unavailable"
    footer_date: str | None
    cited: RetrievedChunk | None = None
    generated: GeneratedAnswer | None = None
    top_similarity: float | None = None
    intent: str | None = None
    scheme_id: str | None = None


def source_date(as_of: str | None, fetched_at: str | None) -> str:
    """The footer date: the document's own date if it states one, else when it was fetched (IST)."""
    if as_of:
        return date.fromisoformat(as_of).strftime(DATE_FORMAT)
    if fetched_at:
        return datetime.fromisoformat(fetched_at).astimezone(IST).strftime(DATE_FORMAT)
    raise ValueError("a source needs as_of or fetched_at")


def format_reply(body: str, url: str, footer_date: str) -> str:
    return f"{body.strip()}\n\nSource: {url}\nLast updated from sources: {footer_date}"


# --- URLs ---------------------------------------------------------------------------------

# Links stop before closing brackets and trailing punctuation, so "(www.x.com)." keeps its ".".
_URL = re.compile(
    r"(?:https?://|www\.)[^\s()<>]*[^\s()<>.,;:!?]"
    r"|\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|in|org|net|gov|co)\b(?:/[^\s()<>]*[^\s()<>.,;:!?])?",
    re.I,
)


def strip_urls(text: str) -> str:
    """Remove links and bare domains ("hdfcfund.com"), then tidy the spaces left behind."""
    text = _URL.sub("", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\s+([.,;:])", r"\1", text).strip()


# --- Sentences ----------------------------------------------------------------------------

_ABBREVIATIONS = ("rs", "no", "e.g", "i.e", "vs", "p.a", "approx", "etc", "re", "st", "mr", "ms", "dr", "inc", "ltd")
_PLACEHOLDER = "\x00"


def count_sentences(text: str) -> int:
    """Count sentences, ignoring decimals ("0.71%") and abbreviations ("Rs. 100", "e.g.").

    Line breaks and bullets also separate sentences, so a list can't sneak past the limit.
    """
    t = text.strip()
    if not t:
        return 0
    t = re.sub(r"(?<=\d)\.(?=\d)", _PLACEHOLDER, t)  # decimals
    for abbr in _ABBREVIATIONS:
        t = re.sub(rf"(?i)\b{re.escape(abbr)}\.", lambda m: m.group(0)[:-1] + _PLACEHOLDER, t)
    parts = re.split(r"[.!?]+(?=\s|$)|\n+|^\s*[-*•]\s+", t, flags=re.M)
    return sum(1 for p in parts if re.search(r"[A-Za-z0-9]", p))


# --- Numeric grounding --------------------------------------------------------------------

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
          "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "thirty": 30}
_NUMBER_WORD = re.compile(r"\b(" + "|".join(_WORDS) + r")\b", re.I)


def _values(text: str) -> set[float]:
    out = set()
    for m in _NUMBER.findall(text):
        try:
            out.add(float(m.replace(",", "").rstrip(".")))
        except ValueError:
            pass
    out.update(float(_WORDS[w.lower()]) for w in _NUMBER_WORD.findall(text))
    return out


def ungrounded_numbers(answer: str, chunk_text: str) -> list[str]:
    """Numbers (including the parts of dates) in `answer` that don't appear in the cited chunk.

    Compared by value, so "1%" matches "1.00%" and "1,000" matches "1000". Number words in the
    chunk ("three years") count too.
    """
    known = _values(chunk_text)
    missing = []
    for m in _NUMBER.findall(answer):
        try:
            value = float(m.replace(",", "").rstrip("."))
        except ValueError:
            continue
        if value not in known:
            missing.append(m)
    return missing


# --- Return figures and advice ------------------------------------------------------------

_RETURN_FIGURE = re.compile(
    r"\bCAGR\b|since\s+inception|\b\d+\s*Y\b|\b\d+[- ]?(?:year|yr)s?\s+returns?\b"
    r"|\breturn(?:s|ed)?\b[^.]{0,30}?\d+(?:\.\d+)?\s*%|\d+(?:\.\d+)?\s*%[^.]{0,30}?\breturns?\b"
    r"|\b(?:annuali[sz]ed|absolute)\s+returns?\b",
    re.I,
)

_ADVISORY = re.compile(
    # Only investment actions count: "you should log in" / "you need to submit" are how-to steps.
    # "you can redeem after 3 years" is a fact, so plain can/could only count before "consider".
    r"\byou\s+(?:can|could)\s+consider\b"
    r"|\byou\s+(?:should|may\s+want\s+to|might\s+want\s+to)\s+(?:also\s+)?"
    r"(?:invest|buy|sell|choose|pick|consider|switch|redeem|stay|hold|avoid|go\s+for|opt|start|stop|continue)\b"
    r"|\b(?:I|we)\s+(?:would\s+)?(?:recommend|suggest|advise)\b"
    r"|\b(?:recommended|suitable|ideal|right|good)\s+for\s+you\b"
    r"|\bbetter\s+(?:option|choice|fund|investment)\b"
    r"|\b(?:good|great|safe|wise)\s+(?:investment|choice|option|fund)\b"
    r"|\bworth\s+investing\b|\bconsider\s+investing\b",
    re.I,
)


def has_return_figures(answer: str) -> bool:
    return bool(_RETURN_FIGURE.search(answer))


def has_advice(answer: str) -> bool:
    """User-directed advice. Quoted official wording such as "investors should note" is allowed."""
    return bool(_ADVISORY.search(answer))


# --- All checks ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    ok: bool
    answer: str = ""  # cleaned answer (URLs stripped) when ok
    cited: RetrievedChunk | None = None
    problem: str = ""  # short reason, also used as the regeneration reminder
    retry: bool = False  # worth regenerating once
    fallback: str = "not_found"  # template to use if it still fails: not_found | performance | advisory


def check(generated: GeneratedAnswer, chunks: list[RetrievedChunk], max_sentences: int = 3) -> Verdict:
    cited = next((c for c in chunks if c.chunk_id == generated.source_chunk_id), None)
    if not generated.answerable or not generated.answer.strip() or cited is None:
        return Verdict(False, problem="no answer or unknown source")
    answer = strip_urls(generated.answer)
    if not answer:
        return Verdict(False, problem="answer was only a link")
    if has_return_figures(answer):
        return Verdict(False, problem="it contained return or performance figures; state no returns",
                       retry=True, fallback="performance")
    if has_advice(answer):
        return Verdict(False, problem="it contained advice; state facts only, with no recommendation",
                       retry=True, fallback="advisory")
    n = count_sentences(answer)
    if n > max_sentences:
        return Verdict(False, problem=f"it had {n} sentences; use at most {max_sentences}", retry=True)
    missing = ungrounded_numbers(answer, cited.text)
    if missing:
        return Verdict(False, problem=f"figures not in the cited excerpt: {', '.join(missing)}")
    return Verdict(True, answer=answer, cited=cited)
