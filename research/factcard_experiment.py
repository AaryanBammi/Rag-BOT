"""Stage 4: add deterministic per-scheme "fact cards" from the factsheet, then hybrid search."""

import re
import sys
from pathlib import Path

import numpy as np

CFG = sys.argv[1] if len(sys.argv) > 1 else "B_200w"
MODEL = "BAAI/bge-small-en-v1.5"
sys.argv = sys.argv[:1]
sys.path.insert(0, str(Path(__file__).parent))
import retrieval_experiment as rx  # noqa: E402
from fastembed import TextEmbedding  # noqa: E402

FIELDS = {
    "Expense ratio": r"EXPENSE RATIO.*?\(As On ([^)]+)\).*?(Regular:\s*[\d.]+%\s*Direct:\s*[\d.]+%)",
    "Benchmark": r"#BENCHMARK INDEX\n([^\n]+(?:\n[^\n#]+\))?)",
    "Exit load": r"EXIT LOAD\$*\n(.*?)(?:\n[A-Z#]{4,}|\.\.\.\.Contd)",
    "Lock-in period": r"LOCK-IN PERIOD\n([^\n]+\n[^\n]+)",
}


def fact_cards():
    d = rx.docs["hdfc-factsheet"]
    by_scheme = {}
    for p in d["pages"]:
        tag = rx.chunkmod.tag_pages((rx.Page(p["number"], p["text"]),), rx.idx)[0]
        if tag:
            by_scheme.setdefault(tag, []).append(p["text"])
    cards = []
    for scheme, pages in by_scheme.items():
        text = "\n".join(pages)
        lines = [f"[Fact card | {rx.names[scheme]} | HDFC MF Factsheet - September 2026]"]
        for label, pat in FIELDS.items():
            m = re.search(pat, text, re.S)
            if m:
                value = " ".join(" ".join(m.groups()).split())
                lines.append(f"{rx.names[scheme]} - {label}: {value}")
        cards.append({"doc_id": "hdfc-factsheet", "type": "factsheet", "scheme": scheme, "text": "\n".join(lines)})
    return cards


target, overlap, clean = rx.ALL_CONFIGS[CFG]
chunks = rx.build_chunks(target, overlap, clean)
cards = fact_cards()
for c in cards:
    print(c["text"].replace("\n", " || ")[:300], "\n")

emb = TextEmbedding(model_name=MODEL, cache_dir=str(rx.MODELS_CACHE_DIR))
D = np.load(rx.OUT / f"emb_{CFG}_{MODEL.replace('/', '_')}.npy")
D = np.vstack([D, np.array(list(emb.passage_embed([c["text"] for c in cards])), dtype=np.float32)])
D /= np.linalg.norm(D, axis=1, keepdims=True)
allc = chunks + cards
bm25 = rx.BM25([c["text"] for c in allc])
Q = {q["id"]: v / np.linalg.norm(v) for q, v in zip(rx.questions, emb.query_embed([q["question"] for q in rx.questions]))}


def hybrid(q, cand):
    dense = list(np.argsort(-(D[cand] @ Q[q["id"]]), kind="stable"))
    sparse = list(np.argsort(-bm25.scores(q["question"], cand), kind="stable"))
    return rx.rrf(dense, sparse)


r = rx.evaluate(allc, hybrid)
print(f"{CFG} + fact cards, hybrid: {r['hit@1']:.2f}/{r['hit@3']:.2f}/{r['hit@5']:.2f} mrr={r['mrr@10']:.2f} misses={r['misses']}")
