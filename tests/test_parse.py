import pytest

from ingest.parse import ParseError, detect_kind, parse, parse_html, parse_pdf
from tests.pdf_fixture import filler, make_pdf


def test_pdf_text_and_page_numbers():
    raw = make_pdf([["HDFC Mid Cap Fund", filler(60, "alpha")], ["EXIT LOAD", "an Exit Load of 1.00% is payable"]])
    doc = parse_pdf(raw)
    assert [p.number for p in doc.pages] == [1, 2]
    assert "Exit Load of 1.00%" in doc.pages[1].text


def test_scanned_pdf_without_text_rejected():
    with pytest.raises(ParseError, match="no extractable text"):
        parse_pdf(make_pdf([[], []]))  # PRS-01


def test_corrupt_pdf_rejected():
    with pytest.raises(ParseError, match="could not read PDF"):
        parse_pdf(b"%PDF-1.4\nthis is not really a pdf")  # PRS-02


def test_kind_detected_from_bytes_not_headers():
    assert detect_kind(make_pdf([["x"]])) == "pdf"  # ING-08
    assert detect_kind(b"  <!DOCTYPE html><html></html>") == "html"
    with pytest.raises(ParseError, match="unrecognised"):
        detect_kind(b"\x89PNG\r\n")


def test_html_main_text_extracted():
    body = "".join(f"<p>The exit load is 1% if redeemed within 1 year. Paragraph {i}.</p>" for i in range(12))
    html = f"<html><head><script>var x=1;</script></head><body><nav>Menu Home</nav><article>{body}</article></body></html>"
    doc = parse_html(html.encode())
    assert "exit load is 1%" in doc.text
    assert "var x" not in doc.text


def test_javascript_shell_rejected():
    shell = b"<html><body><div id='root'></div><script src='app.js'></script></body></html>"
    with pytest.raises(ParseError, match="too little text"):
        parse_html(shell)  # ING-07


def test_parse_dispatches_on_kind():
    assert parse(make_pdf([[filler(80)]])).kind == "pdf"
