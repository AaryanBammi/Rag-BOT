"""Chunking x embedding model x search method, scored on gold-fact retrieval.

A retrieved chunk is a hit when it contains an accepted form of the gold value (plus any
must_also_contain text), comes from an acceptable source type for the question, and belongs to
the asked-about scheme or is a general document. Candidates are filtered like architecture §4.3:
that scheme's chunks + general chunks (general only when the question has no scheme).
"""

import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import yaml
from fastembed import TextEmbedding

import ingest.chunk as chunkmod
from app.config import MODELS_CACHE_DIR
from app.registry import load_registry
from ingest.parse import Page, ParsedDoc

MODELS = sys.argv[1].split(",") if len(sys.argv) > 1 else [
    "BAAI/bge-small-en-v1.5", "BAAI/bge-base-en-v1.5", "snowflake/snowflake-arctic-embed-m",
]
ALL_CONFIGS = {"A_380w": (380, 40, False), "B_200w": (200, 30, False), "C_120w": (120, 20, False),
               "D_200w_clean": (200, 30, True)}
CONFIGS = {k: ALL_CONFIGS[k] for k in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["A_380w", "B_200w", "C_120w"])}
DOT_LEADER = re.compile(r"(?:\s*[.…_]){4,}\s*\d*\s*$")


def clean_text(text):
    """Cleaning candidates from the corpus profile: drop TOC dot-leader lines, join hyphenated breaks."""
    out = []
    for line in text.splitlines():
        if DOT_LEADER.search(line):
            line = DOT_LEADER.sub("", line).strip()
            if len(line.split()) <= 6:  # a bare TOC entry: drop it
                continue
        out.append(line)
    return re.sub(r"(?<=[a-z])-\n(?=[a-z])", "", "\n".join(out))

reg = load_registry()
src_by_id = {s.id: s for s in reg.all_sources()}
names = {s.id: s.name.split(" – ")[0] for s in reg.schemes}
idx = chunkmod.scheme_name_index({s.id: [s.name.split(" – ")[0], *s.aliases] for s in reg.schemes})
docs = {json.loads(p.read_text())["doc_id"]: json.loads(p.read_text()) for p in sorted((ROOT / "data/clean").glob("*.json"))}

gold = yaml.safe_load((ROOT / "tests/gold_facts.yaml").read_text())
questions = [
    q for q in yaml.safe_load((ROOT / "tests/eval_questions.yaml").read_text())
    if q.get("status") != "pending_source" and gold.get(q["expected"].get("gold_fact", ""), {}).get("value")
    and gold[q["expected"]["gold_fact"]]["source_doc"] in docs
]
print(f"{len(questions)} answerable questions, {len(docs)} documents", flush=True)


def build_chunks(target, overlap, clean=False):
    chunkmod.TARGET_WORDS, chunkmod.OVERLAP_WORDS = target, overlap
    out = []
    for doc_id, d in docs.items():
        src = src_by_id[doc_id]
        pages = [(p["number"], clean_text(p["text"]) if clean else p["text"]) for p in d["pages"]]
        pd = ParsedDoc(kind=d["kind"], pages=tuple(Page(n, t) for n, t in pages if t.strip()))
        for c in chunkmod.chunk_document(pd, src.title, src.scheme_id, names, idx if src.multi_scheme else None):
            out.append({"doc_id": doc_id, "type": src.type, "scheme": c.scheme_id or "", "text": c.text})
    return out


TOKEN_RE = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?%?")


def toks(text):
    return TOKEN_RE.findall(text.lower())


class BM25:
    def __init__(self, texts, k1=1.5, b=0.75):
        self.docs = [Counter(toks(t)) for t in texts]
        self.len = np.array([sum(d.values()) for d in self.docs], dtype=float)
        self.avg = self.len.mean()
        df = Counter(w for d in self.docs for w in d)
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.k1, self.b = k1, b

    def scores(self, query, cand):
        q = toks(query)
        s = np.zeros(len(cand))
        for j, i in enumerate(cand):
            d, norm = self.docs[i], self.k1 * (1 - self.b + self.b * self.len[i] / self.avg)
            s[j] = sum(self.idf.get(w, 0) * d[w] * (self.k1 + 1) / (d[w] + norm) for w in q if w in d)
        return s


def is_hit(chunk, q):
    g = gold[q["expected"]["gold_fact"]]
    scheme = q["expected"]["scheme_id"]
    if chunk["type"] not in q["expected"]["source_docs"]:
        return False
    if scheme and chunk["scheme"] not in (scheme, ""):
        return False
    t = chunk["text"].lower()
    return any(a.lower() in t for a in g["accept"]) and all(e.lower() in t for e in g.get("must_also_contain", []))


def candidates(chunks, q):
    s = q["expected"]["scheme_id"]
    return [i for i, c in enumerate(chunks) if c["scheme"] == "" or (s and c["scheme"] == s)]


def evaluate(chunks, rank_fn):
    hits = {1: 0, 3: 0, 5: 0}
    rr = 0.0
    misses = []
    for q in questions:
        cand = candidates(chunks, q)
        order = rank_fn(q, cand)
        ranked = [cand[j] for j in order[:10]]
        first = next((r for r, i in enumerate(ranked, 1) if is_hit(chunks[i], q)), None)
        for k in hits:
            hits[k] += bool(first and first <= k)
        rr += 1 / first if first else 0
        if not first or first > 5:
            misses.append(q["id"])
    n = len(questions)
    return {"hit@1": hits[1] / n, "hit@3": hits[3] / n, "hit@5": hits[5] / n, "mrr@10": rr / n, "misses": misses}


def rrf(*orders, k=60):
    score = Counter()
    for order in orders:
        for rank, j in enumerate(order):
            score[j] += 1 / (k + rank + 1)
    return [j for j, _ in score.most_common()]


def main():
    results_path = OUT / "results.json"
    results = json.loads(results_path.read_text()) if results_path.exists() else []
    for cfg, (target, overlap, clean) in CONFIGS.items():
        chunks = build_chunks(target, overlap, clean)
        texts = [c["text"] for c in chunks]
        bm25 = BM25(texts)
        bm25_rank = lambda q, cand: list(np.argsort(-bm25.scores(q["question"], cand), kind="stable"))
        r = evaluate(chunks, bm25_rank)
        results.append({"config": cfg, "chunks": len(chunks), "model": "-", "method": "bm25", **r})
        print(f"{cfg} chunks={len(chunks)} bm25: {r['hit@1']:.2f}/{r['hit@3']:.2f}/{r['hit@5']:.2f} mrr={r['mrr@10']:.2f}", flush=True)
        for model in MODELS:
            cache = OUT / f"emb_{cfg}_{model.replace('/', '_')}.npy"
            emb = TextEmbedding(model_name=model, cache_dir=str(MODELS_CACHE_DIR))
            t0 = time.time()
            if cache.exists():
                D = np.load(cache)
                secs = None
            else:
                D = np.array(list(emb.passage_embed(texts, batch_size=32)), dtype=np.float32)
                secs = time.time() - t0
                np.save(cache, D)
            D /= np.linalg.norm(D, axis=1, keepdims=True)
            Q = {q["id"]: v / np.linalg.norm(v) for q, v in zip(questions, emb.query_embed([q["question"] for q in questions]))}
            dense_rank = lambda q, cand: list(np.argsort(-(D[cand] @ Q[q["id"]]), kind="stable"))
            hybrid_rank = lambda q, cand: rrf(dense_rank(q, cand), bm25_rank(q, cand))
            for method, fn in (("dense", dense_rank), ("hybrid_rrf", hybrid_rank)):
                r = evaluate(chunks, fn)
                results.append({"config": cfg, "chunks": len(chunks), "model": model, "method": method, "embed_secs": secs, **r})
                print(f"{cfg} {model:38} {method:10} {r['hit@1']:.2f}/{r['hit@3']:.2f}/{r['hit@5']:.2f} mrr={r['mrr@10']:.2f}"
                      f"{'' if secs is None else f'  embed={secs:.0f}s'}", flush=True)

    results_path.write_text(json.dumps(results, indent=1))
    print("saved results.json")


if __name__ == "__main__":
    main()
