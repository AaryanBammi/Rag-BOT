"""Offline tests for generator schema/prompt, footer formatting, and the answer path."""

import pytest

from app import ask as askmod
from app.ask import Answerer
from app.generator import GeneratedAnswer, answer_schema, build_user_prompt
from app.registry import load_registry
from app.retriever import RetrievedChunk
from app.validator import format_reply, source_date
from ingest.manifest import DocumentRow, Manifest


def _chunk(chunk_id="kim:v:1", similarity=0.8, as_of="2025-11-21", url="https://files.hdfcfund.com/kim.pdf"):
    return RetrievedChunk(
        chunk_id=chunk_id, text="Exit Load of 1.00% within 1 year", doc_id="hdfc-mid-cap-kim",
        scheme_id="hdfc-mid-cap", source_type="kim", kind="text", url=url, title="KIM",
        as_of=as_of, fetched_at="2026-10-27T20:00:00+00:00", page=3, score=0.03, similarity=similarity,
    )


def test_schema_restricts_citation_to_retrieved_ids():
    schema = answer_schema(["a:1", "b:2"])
    assert schema["properties"]["source_chunk_id"]["enum"] == ["a:1", "b:2", ""]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"answerable", "answer", "source_chunk_id"}


def test_prompt_labels_excerpts_with_id_type_and_date():
    prompt = build_user_prompt("Exit load?", "HDFC Mid Cap Fund", [_chunk(), _chunk("x:v:2", as_of=None)])
    assert 'id="kim:v:1"' in prompt and 'type="kim"' in prompt and 'as_of="2025-11-21"' in prompt
    assert 'as_of="not stated"' in prompt
    assert "Scheme the question is about: HDFC Mid Cap Fund" in prompt


def test_source_date_prefers_as_of_and_converts_fetch_time_to_ist():
    assert source_date("2026-09-30", "2026-10-27T10:00:00+00:00") == "30 Sep 2026"
    # 20:00 UTC is 01:30 IST the next day (VAL-06).
    assert source_date(None, "2026-10-27T20:00:00+00:00") == "28 Oct 2026"
    with pytest.raises(ValueError):
        source_date(None, None)


def test_format_reply_has_one_link_and_footer():
    text = format_reply("The exit load is 1.00%.", "https://files.hdfcfund.com/kim.pdf", "21 Nov 2025")
    assert text == "The exit load is 1.00%.\n\nSource: https://files.hdfcfund.com/kim.pdf\nLast updated from sources: 21 Nov 2025"


class FakeRetriever:
    def __init__(self, chunks):
        self.chunks = chunks

    def search(self, question, scheme_id=None, top_k=None):
        return self.chunks


@pytest.fixture
def make_answerer(tmp_path):
    manifest = Manifest(tmp_path / "manifest.json")
    manifest.upsert(DocumentRow("hdfc-mid-cap-kim", "u", "kim", "hdfc-mid-cap", "KIM", "h", "2026-10-27T05:00:00+00:00",
                                "2025-11-21", 1, "m", "2026-10-27T05:00:00+00:00"))
    return lambda chunks: Answerer(retriever=FakeRetriever(chunks), registry=load_registry(), manifest=manifest)


def test_answer_cites_chunk_url_and_date(make_answerer, monkeypatch):
    monkeypatch.setattr(askmod, "generate", lambda q, c, s, **kw: GeneratedAnswer(True, "The exit load is 1.00% within 1 year.", "kim:v:1"))
    reply = make_answerer([_chunk()]).ask("Exit load?", "hdfc-mid-cap")
    assert reply.kind == "answer" and reply.url == "https://files.hdfcfund.com/kim.pdf"
    assert reply.text.endswith("Last updated from sources: 21 Nov 2025")


@pytest.mark.parametrize(
    "generated",
    [GeneratedAnswer(False, "", ""), GeneratedAnswer(True, "", "kim:v:1"), GeneratedAnswer(True, "1%", "other:id")],
)
def test_unanswerable_or_bad_citation_gives_not_found(make_answerer, monkeypatch, generated):
    monkeypatch.setattr(askmod, "generate", lambda q, c, s, **kw: generated)
    reply = make_answerer([_chunk()]).ask("Exit load?", "hdfc-mid-cap")
    assert reply.kind == "not_found"
    assert reply.url == load_registry().scheme("hdfc-mid-cap").source("scheme_page").url
    assert reply.footer_date == "27 Oct 2026"  # latest fetch of the scheme's documents, IST


def test_low_similarity_skips_generation(make_answerer, monkeypatch):
    def boom(*a):
        raise AssertionError("generator must not be called below MIN_SIMILARITY")

    monkeypatch.setattr(askmod, "generate", boom)
    reply = make_answerer([_chunk(similarity=0.2)]).ask("How do I bake bread?", None)
    assert reply.kind == "not_found" and "amfiindia.com" in reply.url


def test_generator_is_told_former_scheme_names(make_answerer, monkeypatch):
    seen = {}

    def fake_generate(q, c, scheme, **kw):
        seen["scheme"] = scheme
        return GeneratedAnswer(False, "", "")

    monkeypatch.setattr(askmod, "generate", fake_generate)
    make_answerer([_chunk()]).ask("What is the lock-in for HDFC TaxSaver?", "hdfc-elss-tax-saver")
    assert seen["scheme"].startswith("HDFC ELSS Tax Saver")
    assert "HDFC TaxSaver" in seen["scheme"]
