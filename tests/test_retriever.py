"""Hybrid retriever tests on a small temporary index with the fake embedder."""

import pytest

from app.retriever import BM25Index, Retriever, bm25_tokens, rrf
from ingest.embed import FakeEmbedder
from ingest.store import ChunkStore

CHUNKS = [
    # (id, scheme_id, source_type, kind, text)
    ("mid-ff", "hdfc-mid-cap", "factsheet", "fact_card", "HDFC Mid Cap Fund - Expense ratio: Regular: 1.26% Direct: 0.71%"),
    ("mid-sid", "hdfc-mid-cap", "sid", "text", "The total expense ratio of the scheme is subject to SEBI limits on expenses"),
    ("small-ff", "hdfc-small-cap", "factsheet", "fact_card", "HDFC Small Cap Fund - Expense ratio: Regular: 1.52% Direct: 0.76%"),
    ("sai", "", "sai", "text", "Minimum SIP amount Rs. 100 for daily, weekly and monthly SIP with 6 installments"),
    ("amfi", "", "amfi_education", "text", "ELSS has a lock-in period of 3 years"),
]


@pytest.fixture
def retriever(tmp_path):
    embedder = FakeEmbedder()
    store = ChunkStore(tmp_path / "index", embedder.model)
    store.upsert(
        ids=[c[0] for c in CHUNKS],
        embeddings=embedder.embed_documents([c[4] for c in CHUNKS]),
        documents=[c[4] for c in CHUNKS],
        metadatas=[
            {"doc_id": c[0], "version": "v1", "chunk_index": 0, "scheme_id": c[1], "source_type": c[2],
             "kind": c[3], "url": f"https://files.hdfcfund.com/{c[0]}.pdf", "title": c[0],
             "as_of": "2026-09-30", "fetched_at": "2026-10-27T00:00:00+00:00", "page": 0}
            for c in CHUNKS
        ],
    )
    return Retriever(index_dir=tmp_path / "index", embedder=embedder, top_k=5)


def test_scheme_filter_excludes_other_schemes(retriever):
    results = retriever.search("expense ratio direct plan", scheme_id="hdfc-mid-cap")
    ids = {r.chunk_id for r in results}
    assert "small-ff" not in ids
    assert {"mid-ff", "mid-sid"} <= ids  # scheme chunks
    assert {"sai", "amfi"} <= ids  # general chunks are always candidates


def test_no_scheme_searches_general_only(retriever):
    ids = {r.chunk_id for r in retriever.search("What is the ELSS lock-in period?")}
    assert ids == {"sai", "amfi"}


def test_exact_figures_rank_first(retriever):
    top = retriever.search("expense ratio direct 0.71%", scheme_id="hdfc-mid-cap")[0]
    assert top.chunk_id == "mid-ff" and top.kind == "fact_card"


def test_result_fields(retriever):
    r = retriever.search("minimum SIP amount", scheme_id=None)[0]
    assert r.chunk_id == "sai" and r.scheme_id is None and r.page is None
    assert r.as_of == "2026-09-30" and r.url.endswith("sai.pdf")
    assert -1.0 <= r.similarity <= 1.0


def test_top_k_limits_results(retriever):
    assert len(retriever.search("expense ratio", scheme_id="hdfc-mid-cap", top_k=2)) == 2


def test_reranker_hook_reorders(retriever):
    retriever.reranker = lambda q, texts: [1.0 if "lock-in" in t else 0.0 for t in texts]
    assert retriever.search("minimum SIP", scheme_id=None)[0].chunk_id == "amfi"


def test_index_built_with_other_model_rejected(tmp_path):
    ChunkStore(tmp_path / "index", "voyage-4")
    with pytest.raises(RuntimeError, match="--rebuild"):
        Retriever(index_dir=tmp_path / "index", embedder=FakeEmbedder())


def test_bm25_tokens_keep_decimals_and_percent():
    assert bm25_tokens("Direct: 0.71% (Rs. 100)") == ["direct", "0.71%", "rs", "100"]


def test_bm25_prefers_matching_terms():
    index = BM25Index(["exit load 1%", "benchmark nifty midcap 150", "exit load nil"])
    scores = index.scores("nifty benchmark", [0, 1, 2])
    assert scores.argmax() == 1 and scores[0] == scores[2] == 0


def test_rrf_rewards_agreement():
    fused = [item for item, _ in rrf([1, 2, 3], [3, 1, 2])]
    assert fused[0] == 1
