from ingest.chunk import (
    OVERLAP_WORDS,
    TARGET_WORDS,
    chunk_document,
    scheme_name_index,
    tag_pages,
)
from ingest.parse import Page, ParsedDoc

NAMES = {"hdfc-mid-cap": "HDFC Mid Cap Fund", "hdfc-large-cap": "HDFC Large Cap Fund"}
INDEX = scheme_name_index(
    {
        "hdfc-mid-cap": ["HDFC Mid Cap Fund", "HDFC Mid-Cap Opportunities Fund"],
        "hdfc-large-cap": ["HDFC Large Cap Fund", "HDFC Top 100 Fund"],
    }
)


def _doc(*page_texts: str) -> ParsedDoc:
    return ParsedDoc(kind="pdf", pages=tuple(Page(i, t) for i, t in enumerate(page_texts, start=1)))


def _lines(n: int, words_per_line: int = 10) -> str:
    return "\n".join(" ".join(f"w{i}x{j}" for j in range(words_per_line)) for i in range(n))


def test_chunks_respect_word_budget_and_overlap():
    doc = _doc(_lines(120))  # 1200 words
    chunks = chunk_document(doc, "SID", "hdfc-mid-cap", NAMES)
    assert len(chunks) >= 3
    for c in chunks:
        body = c.text.split("\n", 1)[1]
        assert len(body.split()) <= TARGET_WORDS
    # The last lines of one chunk start the next one.
    first_body = chunks[0].text.split("\n")[1:]
    second_body = chunks[1].text.split("\n")[1:]
    assert second_body[0] in first_body
    assert sum(len(l.split()) for l in second_body[: first_body[::-1].index(second_body[0]) + 1]) >= OVERLAP_WORDS - 10


def test_lines_are_never_split():
    row = "Exit load | 1.00% if redeemed within 1 year"
    doc = _doc(_lines(35) + "\n" + row + "\n" + _lines(35))
    assert any(row in c.text for c in chunk_document(doc, "KIM", "hdfc-mid-cap", NAMES))


def test_context_header_names_document_scheme_and_page():
    chunks = chunk_document(_doc("hello world"), "HDFC Mid Cap Fund - kim", "hdfc-mid-cap", NAMES)
    assert chunks[0].text.startswith("[HDFC Mid Cap Fund - kim | HDFC Mid Cap Fund | page 1]")
    assert chunks[0].scheme_id == "hdfc-mid-cap"


def test_heading_starts_new_chunk_when_reasonably_full():
    doc = _doc(_lines(25) + "\nEXIT LOAD\nan Exit Load of 1.00% is payable")
    chunks = chunk_document(doc, "SID", "hdfc-mid-cap", NAMES)
    assert any(c.text.split("\n")[1:][0] == "EXIT LOAD" or "\nEXIT LOAD\n" in c.text for c in chunks[1:])


def test_multi_scheme_pages_tagged_by_exact_heading():
    pages = (
        Page(1, "Index of schemes\nHDFC Mid Cap Fund 13\nHDFC Large Cap Fund 11"),  # TOC: no exact line
        Page(2, "12 | September 2026\nHDFC Large Cap Fund\nAn open ended equity scheme"),
        Page(3, "For Product label...\n13 | September 2026\nHDFC Mid Cap Fund\nMID CAP FUND"),
        Page(4, "14 | September 2026\nHDFC Large and Mid Cap Fund\nLARGE & MID CAP"),  # not Large Cap
        Page(5, "HDFC Mid-Cap Opportunities Fund\nformer name heading"),  # alias
    )
    assert tag_pages(pages, INDEX) == [None, "hdfc-large-cap", "hdfc-mid-cap", None, "hdfc-mid-cap"]


def test_multi_scheme_document_drops_untagged_pages():
    doc = _doc(
        "Annexure: all schemes\nHDFC Value Fund Rs. 100",
        "HDFC Mid Cap Fund\nEXPENSE RATIO Regular: 1.26% Direct: 0.71%",
        "15 | September 2026\nHDFC Mid Cap Fund\nperformance table",
        "HDFC Small Cap Fund\nnot in this test registry",
    )
    chunks = chunk_document(doc, "Factsheet", None, NAMES, multi_scheme_index=INDEX)
    assert {c.scheme_id for c in chunks} == {"hdfc-mid-cap"}
    assert all("Annexure" not in c.text and "Small Cap" not in c.text for c in chunks)
    assert chunks[0].text.startswith("[Factsheet | HDFC Mid Cap Fund | page 2]")


def test_duplicate_alias_across_schemes_rejected():
    import pytest

    with pytest.raises(ValueError, match="used by both"):
        scheme_name_index({"a": ["HDFC Fund"], "b": ["HDFC  fund"]})


def test_token_cap_limits_chunks_below_model_limit():
    # Few words but many tokens per line, like table-of-contents dot leaders.
    doc = _doc("\n".join("SECTION " + "." * 300 for _ in range(10)))
    count = lambda t: len(t) // 4 + 1
    chunks = chunk_document(doc, "SID", "hdfc-mid-cap", NAMES, count_tokens=count, max_tokens=200)
    assert len(chunks) > 1
    assert all(count(c.text) <= 200 for c in chunks)


def test_single_line_over_cap_is_split_by_words():
    doc = _doc(" ".join(f"word{i}" for i in range(600)))
    count = lambda t: len(t.split())
    chunks = chunk_document(doc, "SID", "hdfc-mid-cap", NAMES, count_tokens=count, max_tokens=100)
    assert all(count(c.text) <= 100 for c in chunks)
    assert "word599" in chunks[-1].text


def test_default_kind_is_text():
    assert chunk_document(_doc("hello world"), "KIM", "hdfc-mid-cap", NAMES)[0].kind == "text"


def test_numbered_section_heading_starts_new_chunk():
    from ingest.chunk import _is_heading

    assert _is_heading("11. Minimum Application Amount / Number of Units")
    assert _is_heading("XV. Segregated portfolio/side pocketing disclosure")
    assert not _is_heading("1. In respect of valid applications received upto 3 p.m. on a Business Day by the Fund")
    assert not _is_heading("2.5% of the NAV")
    doc = _doc(_lines(15) + "\n11. Minimum Application Amount / Number of Units\nRs.100/- and any amount thereafter")
    chunks = chunk_document(doc, "KIM", "hdfc-mid-cap", NAMES)
    section = next(c for c in chunks if "Rs.100/-" in c.text)
    assert "11. Minimum Application Amount" in section.text
