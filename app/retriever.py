"""Hybrid retriever (architecture.md §4.3, Docs/data_research.md §1).

Dense search (Chroma, cosine) and BM25 keyword search run over the same candidate set, the
asked-about scheme's chunks plus general chunks (general only when no scheme is known), and
their rankings are merged with reciprocal rank fusion (RRF). An optional cross-encoder reranker
can reorder the fused top results; it is off by default because the best one measured
(bge-reranker-v2-m3) takes ~11 s per question on CPU.

Each result carries the chunk's dense cosine similarity, which the not-found check compares
with MIN_SIMILARITY (Phase 2).

Usage: python -m app.retriever "question" [--scheme hdfc-mid-cap] [--k 5]
"""

from __future__ import annotations

import argparse
import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import chromadb
import numpy as np

from app.config import MODELS_CACHE_DIR, settings
from ingest.embed import Embedder, get_embedder
from ingest.store import COLLECTION

CANDIDATE_POOL = 50  # results taken from each of dense and BM25 before fusion
RRF_K = 60
RERANK_TOP_N = 20

Reranker = Callable[[str, list[str]], list[float]]


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    text: str
    doc_id: str
    scheme_id: str | None
    source_type: str
    kind: str  # "text" or "fact_card"
    url: str
    title: str
    as_of: str | None
    fetched_at: str
    page: int | None
    score: float  # fused RRF score, or reranker score when reranking is on
    similarity: float  # dense cosine similarity to the query


_TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?%?")


def bm25_tokens(text: str) -> list[str]:
    """Lowercase words and numbers, keeping decimals and % together ("0.71%")."""
    return _TOKEN_RE.findall(text.lower())


class BM25Index:
    def __init__(self, texts: list[str], k1: float = 1.5, b: float = 0.75) -> None:
        self._docs = [Counter(bm25_tokens(t)) for t in texts]
        self._len = np.array([sum(d.values()) for d in self._docs], dtype=float)
        self._avg = float(self._len.mean()) if len(self._docs) else 0.0
        df = Counter(w for d in self._docs for w in d)
        n = len(self._docs)
        self._idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query: str, candidates: list[int]) -> np.ndarray:
        terms = bm25_tokens(query)
        out = np.zeros(len(candidates))
        for j, i in enumerate(candidates):
            doc = self._docs[i]
            norm = self.k1 * (1 - self.b + self.b * self._len[i] / (self._avg or 1))
            out[j] = sum(
                self._idf.get(w, 0.0) * doc[w] * (self.k1 + 1) / (doc[w] + norm) for w in terms if w in doc
            )
        return out


def rrf(*rankings: list[int], k: int = RRF_K) -> list[tuple[int, float]]:
    """Reciprocal rank fusion: [(item, score)] best first."""
    scores: Counter = Counter()
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            scores[item] += 1.0 / (k + rank + 1)
    return scores.most_common()


def make_reranker(model: str) -> Reranker:
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    encoder = TextCrossEncoder(model_name=model, cache_dir=str(MODELS_CACHE_DIR))
    return lambda query, texts: [float(s) for s in encoder.rerank(query, texts)]


class Retriever:
    def __init__(
        self,
        index_dir: Path | None = None,
        embedder: Embedder | None = None,
        top_k: int | None = None,
        reranker: Reranker | None = None,
    ) -> None:
        self.embedder = embedder or get_embedder()
        self.top_k = top_k or settings.retrieval_top_k
        if reranker is None and settings.reranker_model:
            reranker = make_reranker(settings.reranker_model)
        self.reranker = reranker
        client = chromadb.PersistentClient(path=str(index_dir or settings.index_dir))
        self._col = client.get_collection(COLLECTION, embedding_function=None)
        stored_model = (self._col.metadata or {}).get("embedding_model")
        if stored_model != self.embedder.model:
            raise RuntimeError(
                f"Index was built with {stored_model!r} but the embedder is {self.embedder.model!r}; "
                "re-run `python -m ingest --rebuild`."
            )
        self.refresh()

    def refresh(self) -> None:
        """(Re)load chunk texts, metadata and vectors, and rebuild the BM25 index."""
        data = self._col.get(include=["documents", "metadatas", "embeddings"])
        self._ids: list[str] = list(data["ids"])
        self._texts: list[str] = list(data["documents"])
        self._metas: list[dict] = list(data["metadatas"])
        vectors = np.asarray(data["embeddings"], dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True) if len(vectors) else 1
        self._vectors = vectors / np.where(norms == 0, 1, norms)
        self._pos = {chunk_id: i for i, chunk_id in enumerate(self._ids)}
        self._bm25 = BM25Index(self._texts)

    def __len__(self) -> int:
        return len(self._ids)

    @staticmethod
    def _where(scheme_id: str | None) -> dict:
        if scheme_id:
            return {"$or": [{"scheme_id": scheme_id}, {"scheme_id": ""}]}
        return {"scheme_id": ""}

    def _candidates(self, scheme_id: str | None) -> list[int]:
        allowed = {scheme_id, ""} if scheme_id else {""}
        return [i for i, m in enumerate(self._metas) if m.get("scheme_id", "") in allowed]

    def search(self, query: str, scheme_id: str | None = None, top_k: int | None = None) -> list[RetrievedChunk]:
        top_k = top_k or self.top_k
        candidates = self._candidates(scheme_id)
        if not candidates:
            return []

        qvec = np.asarray(self.embedder.embed_query(query), dtype=np.float32)
        qvec /= np.linalg.norm(qvec) or 1.0

        dense = self._col.query(
            query_embeddings=[qvec.tolist()],
            n_results=min(CANDIDATE_POOL, len(candidates)),
            where=self._where(scheme_id),
            include=[],
        )
        dense_ranking = [self._pos[i] for i in dense["ids"][0] if i in self._pos]

        bm25_scores = self._bm25.scores(query, candidates)
        order = np.argsort(-bm25_scores, kind="stable")[:CANDIDATE_POOL]
        bm25_ranking = [candidates[j] for j in order if bm25_scores[j] > 0]

        fused = rrf(dense_ranking, bm25_ranking)
        if self.reranker is not None:
            pool = [i for i, _ in fused[:RERANK_TOP_N]]
            scores = self.reranker(query, [self._texts[i] for i in pool])
            fused = sorted(zip(pool, scores), key=lambda x: -x[1])

        return [self._result(i, score, qvec) for i, score in fused[:top_k]]

    def _result(self, i: int, score: float, qvec: np.ndarray) -> RetrievedChunk:
        m = self._metas[i]
        return RetrievedChunk(
            chunk_id=self._ids[i],
            text=self._texts[i],
            doc_id=m["doc_id"],
            scheme_id=m.get("scheme_id") or None,
            source_type=m.get("source_type", ""),
            kind=m.get("kind", "text"),
            url=m.get("url", ""),
            title=m.get("title", ""),
            as_of=m.get("as_of") or None,
            fetched_at=m.get("fetched_at", ""),
            page=m.get("page") or None,
            score=float(score),
            similarity=float(self._vectors[i] @ qvec),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.retriever")
    parser.add_argument("question")
    parser.add_argument("--scheme")
    parser.add_argument("--k", type=int, default=None)
    args = parser.parse_args(argv)
    retriever = Retriever()
    for r in retriever.search(args.question, args.scheme, args.k):
        page = f" p{r.page}" if r.page else ""
        print(f"--- {r.doc_id}{page} [{r.source_type}/{r.kind}] score={r.score:.4f} sim={r.similarity:.3f}")
        print(r.text[:400] + ("…" if len(r.text) > 400 else ""), "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
