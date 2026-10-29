"""Stage 3: hybrid top-20 -> source-type priority and/or cross-encoder rerank. Reuses cached embeddings.

usage: rerank_experiment.py <config> <embedding model>
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.argv, (CFG, MODEL) = sys.argv[:1], sys.argv[1:3]
sys.path.insert(0, str(Path(__file__).parent))
import retrieval_experiment as rx  # noqa: E402  (module-level run skipped: no configs passed)
from fastembed import TextEmbedding  # noqa: E402
from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: E402

RERANKERS = ["Xenova/ms-marco-MiniLM-L-12-v2", "BAAI/bge-reranker-v2-m3-int8"]
TOP_N = 20
# Planned source priority (architecture §4.3): for changing figures prefer factsheet/fund facts/scheme page.
PRIORITY = {"factsheet": 0.02, "fund_facts": 0.02, "scheme_page": 0.02}

target, overlap, clean = rx.ALL_CONFIGS[CFG]
chunks = rx.build_chunks(target, overlap, clean)
texts = [c["text"] for c in chunks]
bm25 = rx.BM25(texts)
D = np.load(rx.OUT / f"emb_{CFG}_{MODEL.replace('/', '_')}.npy")
D /= np.linalg.norm(D, axis=1, keepdims=True)
emb = TextEmbedding(model_name=MODEL, cache_dir=str(rx.MODELS_CACHE_DIR))
Q = {q["id"]: v / np.linalg.norm(v) for q, v in zip(rx.questions, emb.query_embed([q["question"] for q in rx.questions]))}


def hybrid(q, cand):
    dense = list(np.argsort(-(D[cand] @ Q[q["id"]]), kind="stable"))
    sparse = list(np.argsort(-bm25.scores(q["question"], cand), kind="stable"))
    return rx.rrf(dense, sparse)


def with_priority(q, cand):
    order = hybrid(q, cand)
    base = {j: 1 / (60 + r + 1) for r, j in enumerate(order)}
    return sorted(order, key=lambda j: -(base[j] + PRIORITY.get(chunks[cand[j]]["type"], 0)))


def report(name, fn):
    r = rx.evaluate(chunks, fn)
    print(f"{CFG} {MODEL:36} {name:28} {r['hit@1']:.2f}/{r['hit@3']:.2f}/{r['hit@5']:.2f} mrr={r['mrr@10']:.2f} misses={r['misses']}", flush=True)
    return r


report("hybrid", hybrid)
report("hybrid+priority", with_priority)
for name in RERANKERS:
    ce = TextCrossEncoder(model_name=name, cache_dir=str(rx.MODELS_CACHE_DIR))
    timings = []

    def reranked(q, cand, base=hybrid, ce=ce):
        top = base(q, cand)[:TOP_N]
        t0 = time.time()
        scores = list(ce.rerank(q["question"], [chunks[cand[j]]["text"] for j in top]))
        timings.append(time.time() - t0)
        return [top[i] for i in np.argsort(-np.array(scores), kind="stable")]

    report(f"hybrid+rerank[{name.split('/')[1][:14]}]", reranked)
    print(f"   rerank latency per query (top {TOP_N}): median {np.median(timings) * 1000:.0f} ms", flush=True)
