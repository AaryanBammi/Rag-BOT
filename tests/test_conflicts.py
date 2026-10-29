"""Conflict rule: for changing figures, the most recent source wins (Phase 2 exit criterion)."""

from dataclasses import replace

from app import ask as askmod
from app.conflicts import prefer_latest, question_topics, states_figure
from app.generator import GeneratedAnswer
from app.retriever import RetrievedChunk
from tests.answer_eval import normalise

# Real text from the Mid Cap KIM (Nov 2025) and fund facts (Sep 2026).
KIM_TURNOVER = "• Portfolio Turnover Ratio (September 30, 2025)\nWithout Derivatives - 14.39%\nWith Derivatives - 14.39%"
FACTS_TURNOVER = "Portfolio Turnover Ratio\nEquity Turnover 2.87%\nTotal Turnover 2.87%"
KIM_EXIT_LOAD = "Exit Load: 1.00% if redeemed within 1 year from the date of allotment."
GLOSSARY_TER = "Total expense ratio (TER) means the ratio of total of all expenses charged to the investors of the scheme"


def _c(chunk_id, text, as_of, scheme_id="hdfc-mid-cap", source_type="kim"):
    return RetrievedChunk(chunk_id=chunk_id, text=text, doc_id=chunk_id.split(":")[0], scheme_id=scheme_id,
                          source_type=source_type, kind="text", url=f"https://files.hdfcfund.com/{chunk_id}.pdf",
                          title=chunk_id, as_of=as_of, fetched_at="2026-10-27T05:00:00+00:00", page=1,
                          score=0.03, similarity=0.8)


def test_topics_detected_only_for_changing_figures():
    assert question_topics("What is the expense ratio of HDFC Mid Cap?") == ["expense_ratio"]
    assert question_topics("What is the portfolio turnover ratio?") == ["portfolio_turnover"]
    assert question_topics("What is the AUM?") == ["aum"]
    assert question_topics("What is the exit load?") == []
    assert question_topics("What is the minimum SIP?") == []


def test_states_figure_needs_a_number_near_the_label():
    assert states_figure(KIM_TURNOVER, "portfolio_turnover")
    assert states_figure("Sept 30, 2026\n₹101,856.06Cr.\nAUM", "aum")
    assert not states_figure(GLOSSARY_TER, "expense_ratio")


def test_older_figure_dropped_and_order_kept():
    kim = _c("kim:1", KIM_TURNOVER, "2025-11-21")
    facts = _c("facts:1", FACTS_TURNOVER, "2026-09-30", source_type="fund_facts")
    other = _c("kim:2", KIM_EXIT_LOAD, "2025-11-21")
    out = prefer_latest("What is the portfolio turnover ratio of HDFC Mid Cap Fund?", [kim, other, facts])
    assert [c.chunk_id for c in out] == ["kim:2", "facts:1"]


def test_static_terms_untouched():
    chunks = [_c("kim:1", KIM_EXIT_LOAD, "2025-11-21"), _c("facts:1", "Exit load 1.00% within 1 year", "2026-09-30")]
    assert prefer_latest("What is the exit load?", chunks) == chunks


def test_general_and_undated_chunks_never_dropped():
    kim = _c("kim:1", "Total Expense Ratio 0.76% p.a.", "2025-11-21")
    facts = _c("facts:1", "Total Expense Ratio Direct 0.71%", "2026-09-30", source_type="fund_facts")
    sai = _c("sai:1", "Total expense ratio limit 2.25% p.a.", "2026-06-30", scheme_id=None, source_type="sai")
    undated = _c("x:1", "expense ratio 1.5%", None)
    out = prefer_latest("expense ratio?", [kim, sai, undated, facts])
    assert [c.chunk_id for c in out] == ["sai:1", "x:1", "facts:1"]


def test_single_stating_chunk_kept_even_if_old():
    kim = _c("kim:1", KIM_TURNOVER, "2025-11-21")
    assert prefer_latest("portfolio turnover?", [kim]) == [kim]


def test_answer_path_only_shows_newest_figure_to_model(monkeypatch, tmp_path):
    from app.ask import Answerer
    from app.registry import load_registry
    from ingest.manifest import Manifest

    kim = _c("kim:1", KIM_TURNOVER, "2025-11-21")
    facts = _c("facts:1", FACTS_TURNOVER, "2026-09-30", source_type="fund_facts")
    seen = {}

    def fake_generate(q, chunks, scheme, **kw):
        seen["ids"] = [c.chunk_id for c in chunks]
        return GeneratedAnswer(True, "The total portfolio turnover ratio is 2.87%.", "facts:1")

    class R:
        def search(self, q, scheme_id=None, top_k=None):
            return [kim, facts]

    monkeypatch.setattr(askmod, "generate", fake_generate)
    reply = Answerer(R(), load_registry(), Manifest(tmp_path / "m.json")).ask("Portfolio turnover ratio?", "hdfc-mid-cap")
    assert seen["ids"] == ["facts:1"]
    assert reply.kind == "answer" and reply.footer_date == "30 Sep 2026"


def test_grader_treats_narrow_spaces_as_spaces():
    assert "rs. 100" in normalise("The minimum SIP amount is Rs. 100.")
    assert normalise("₹ 100") == "₹ 100"
