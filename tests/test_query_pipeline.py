"""Query pipeline with mock classifier/generator: routing, PII, fallbacks, retry cap (eval.md Phase 3)."""

import re

import pytest

from app import ask as askmod
from app import llm
from app.ask import Answerer
from app.classifier import Classification, _parse, classifier_schema, system_prompt, user_prompt
from app.generator import GeneratedAnswer
from app.llm import LLMError
from app.pipeline import Pipeline
from app.registry import load_registry
from app.retriever import RetrievedChunk
from app.templates import ADVISORY_ASIDE
from ingest.manifest import DocumentRow, Manifest
from tests.test_pii_guard import POSITIVES

REG = load_registry()
CHUNK = RetrievedChunk(
    chunk_id="kim:1", text="Exit Load: 1.00% if redeemed within 1 year from the date of allotment.",
    doc_id="hdfc-small-cap-kim", scheme_id="hdfc-small-cap", source_type="kim", kind="text",
    url="https://files.hdfcfund.com/kim.pdf", title="KIM", as_of="2025-11-21",
    fetched_at="2026-10-27T05:00:00+00:00", page=3, score=0.03, similarity=0.8)


class Retriever:
    calls = 0

    def search(self, q, scheme_id=None, top_k=None):
        Retriever.calls += 1
        return [CHUNK]


def route(intent="factual", scheme_id="hdfc-small-cap", needs_scheme=False, aside=False, query="Exit load?"):
    return lambda message, registry, history=(): Classification(intent, scheme_id, needs_scheme, aside, query)


@pytest.fixture
def pipeline(tmp_path):
    manifest = Manifest(tmp_path / "m.json")
    for s in REG.all_sources():
        if s.type not in {"scheme_page", "amc_service"}:
            manifest.upsert(DocumentRow(s.id, s.url, s.type, s.scheme_id, s.title, "h",
                                        "2026-10-27T05:00:00+00:00", None, 1, "m", "2026-10-27T05:00:00+00:00"))
    answerer = Answerer(Retriever(), REG, manifest)
    return lambda classifier: Pipeline(answerer, classifier)


@pytest.fixture
def generator(monkeypatch):
    """Queue of generator outputs; records each call."""
    state = {"outputs": [], "calls": []}

    def fake(q, chunks, scheme, **kw):
        state["calls"].append(kw)
        return state["outputs"].pop(0)

    monkeypatch.setattr(askmod, "generate", fake)
    return state


def links(text):
    return len(re.findall(r"https?://", text))


@pytest.mark.parametrize("kind,message", POSITIVES)
def test_pii_never_reaches_a_model(pipeline, monkeypatch, kind, message):
    def boom(*a, **k):
        raise AssertionError("model called for a PII message")

    monkeypatch.setattr(llm, "complete_json", boom)
    monkeypatch.setattr(askmod, "generate", boom)
    Retriever.calls = 0
    reply = pipeline(boom).handle(message)
    assert reply.kind == "pii" and links(reply.text) == 1 and Retriever.calls == 0
    assert reply.url == REG.general_of_type("investor_safety")[0].url


@pytest.mark.parametrize("intent,scheme,expected", [
    ("advisory", "hdfc-mid-cap", "advisory"),
    ("greeting", None, "greeting"),
    ("out_of_scope", None, "out_of_scope"),
    ("multi_scheme", None, "multi_scheme"),
    ("performance", "hdfc-mid-cap", "performance"),
    ("performance", None, "performance"),
    ("nav", "hdfc-mid-cap", "nav"),
    ("nav", None, "clarification"),
])
def test_templates_by_intent_without_generation(pipeline, generator, intent, scheme, expected):
    reply = pipeline(route(intent, scheme)).handle("message")
    assert reply.kind == expected and links(reply.text) == 1 and reply.intent == intent
    assert generator["calls"] == []


def test_needs_scheme_asks_which_scheme(pipeline, generator):
    reply = pipeline(route(scheme_id=None, needs_scheme=True)).handle("What is the exit load?")
    assert reply.kind == "clarification" and generator["calls"] == []


def test_factual_answer_with_advisory_aside(pipeline, generator):
    generator["outputs"] = [GeneratedAnswer(True, "The exit load is 1.00% within 1 year.", "kim:1")]
    reply = pipeline(route(aside=True)).handle("Exit load of HDFC Small Cap, and should I invest?")
    assert reply.kind == "answer"
    assert reply.text.startswith(f"The exit load is 1.00% within 1 year. {ADVISORY_ASIDE}\n\nSource: ")
    assert generator["calls"][0]["max_sentences"] == 2 and links(reply.text) == 1


def test_ungrounded_figure_never_reaches_user(pipeline, generator):
    generator["outputs"] = [GeneratedAnswer(True, "The exit load is 2.00% within 1 year.", "kim:1")]
    reply = pipeline(route()).handle("Exit load?")
    assert reply.kind == "not_found" and "2.00" not in reply.text and len(generator["calls"]) == 1


def test_return_figure_regenerates_once_then_performance_template(pipeline, generator):
    bad = GeneratedAnswer(True, "The 1-year return is 1.00%.", "kim:1")
    generator["outputs"] = [bad, bad]
    reply = pipeline(route()).handle("Exit load?")
    assert reply.kind == "performance" and "1.00" not in reply.text
    assert len(generator["calls"]) == 2 and "return" in generator["calls"][1]["reminder"]


def test_advice_regenerates_once_then_succeeds(pipeline, generator):
    generator["outputs"] = [GeneratedAnswer(True, "You should invest; exit load is 1.00%.", "kim:1"),
                            GeneratedAnswer(True, "The exit load is 1.00%.", "kim:1")]
    reply = pipeline(route()).handle("Exit load?")
    assert reply.kind == "answer" and len(generator["calls"]) == 2


def test_at_most_three_model_calls(pipeline, generator):
    calls = {"classifier": 0}

    def classifier(message, registry, history=()):
        calls["classifier"] += 1
        return Classification("factual", "hdfc-small-cap", False, False, "Exit load?")

    long = GeneratedAnswer(True, "One. Two. Three. Four.", "kim:1")
    generator["outputs"] = [long, long, long]
    reply = pipeline(classifier).handle("Exit load?")
    assert reply.kind == "not_found" and calls["classifier"] + len(generator["calls"]) == 3


def test_llm_failure_gives_service_unavailable(pipeline):
    def fail(*a, **k):
        raise LLMError("rate_limited", "429")

    reply = pipeline(fail).handle("Exit load?")
    assert reply.kind == "service_unavailable" and reply.url is None


def test_classifier_schema_and_prompt():
    schema = classifier_schema(REG.scheme_ids)
    assert schema["properties"]["scheme_id"]["enum"] == [*REG.scheme_ids, ""]
    prompt = system_prompt(REG)
    assert "HDFC TaxSaver" in prompt and "hdfc-silver-etf-fof" in prompt
    assert "user: hi" in user_prompt("what is its exit load?", [("user", "hi")])


def test_classifier_parse_normalises_flags():
    c = _parse({"intent": "advisory", "scheme_id": "", "needs_scheme": True, "advisory_aside": True,
                "standalone_query": ""}, "msg")
    assert c.scheme_id is None and c.advisory_aside is False and c.standalone_query == "msg"
    c = _parse({"intent": "factual", "scheme_id": "hdfc-mid-cap", "needs_scheme": True, "advisory_aside": True,
                "standalone_query": "q"}, "msg")
    assert c.needs_scheme is False and c.advisory_aside is True
