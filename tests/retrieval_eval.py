"""Retrieval eval on the real index: `python -m tests.retrieval_eval [--k 5] [--show-misses]`.

Scores every answerable factual eval question with the same criteria as Docs/data_research.md §3:
a retrieved chunk is a hit when it contains an accepted form of the gold value (plus any
must_also_contain text) and comes from an acceptable source type for the question. Questions
are searched as written (no classifier rewrite), with the question's scheme as the filter.
"""

from __future__ import annotations

import argparse
import sys
import time

import yaml

from app.config import PROJECT_ROOT
from app.retriever import RetrievedChunk, Retriever

GOLD = yaml.safe_load((PROJECT_ROOT / "tests" / "gold_facts.yaml").read_text(encoding="utf-8"))
QUESTIONS = yaml.safe_load((PROJECT_ROOT / "tests" / "eval_questions.yaml").read_text(encoding="utf-8"))


def answerable() -> list[dict]:
    return [
        q for q in QUESTIONS
        if q.get("category") == "factual" and q.get("status") != "pending_source"
        and GOLD.get(q["expected"].get("gold_fact", ""), {}).get("value")
    ]


def is_hit(chunk: RetrievedChunk, q: dict) -> bool:
    gold = GOLD[q["expected"]["gold_fact"]]
    if chunk.source_type not in q["expected"]["source_docs"]:
        return False
    text = chunk.text.lower()
    return any(a.lower() in text for a in gold.get("chunk_accept") or gold["accept"]) and all(
        e.lower() in text for e in gold.get("must_also_contain", [])
    )


def evaluate(retriever: Retriever, k: int = 10) -> dict:
    questions = answerable()
    hits = {1: 0, 3: 0, 5: 0}
    rr, misses, latencies = 0.0, [], []
    for q in questions:
        t0 = time.perf_counter()
        results = retriever.search(q["question"], q["expected"]["scheme_id"], top_k=k)
        latencies.append(time.perf_counter() - t0)
        first = next((rank for rank, r in enumerate(results, 1) if is_hit(r, q)), None)
        for n in hits:
            hits[n] += bool(first and first <= n)
        rr += 1 / first if first else 0
        if not first or first > 5:
            misses.append((q["id"], first))
    n = len(questions)
    latencies.sort()
    return {
        "questions": n,
        "hit@1": hits[1] / n, "hit@3": hits[3] / n, "hit@5": hits[5] / n, "mrr@10": rr / n,
        "misses": misses,
        "latency_ms_median": 1000 * latencies[n // 2], "latency_ms_p95": 1000 * latencies[int(0.95 * (n - 1))],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.retrieval_eval")
    parser.add_argument("--min-hit5", type=float, default=0.95, help="exit non-zero below this hit@5")
    args = parser.parse_args(argv)
    retriever = Retriever()
    r = evaluate(retriever)
    print(f"{r['questions']} questions, {len(retriever)} chunks")
    print(f"hit@1 {r['hit@1']:.2f}  hit@3 {r['hit@3']:.2f}  hit@5 {r['hit@5']:.2f}  MRR@10 {r['mrr@10']:.2f}")
    print(f"latency median {r['latency_ms_median']:.0f} ms, p95 {r['latency_ms_p95']:.0f} ms")
    for qid, rank in r["misses"]:
        print(f"  miss: {qid} (first hit rank: {rank or 'none in top 10'})")
    return 0 if r["hit@5"] >= args.min_hit5 else 1


if __name__ == "__main__":
    sys.exit(main())
