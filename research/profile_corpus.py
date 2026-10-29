"""Profile data/clean: sizes, token lengths, structure, boilerplate, artifacts, fact locations."""

import collections
import glob
import json
import re
import statistics as st
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from tokenizers import Tokenizer

from ingest.chunk import chunk_document
from ingest.parse import Page, ParsedDoc
from app.registry import load_registry

tok_path = next(Path(ROOT / ".cache/fastembed").rglob("tokenizer.json"))
TOK = Tokenizer.from_file(str(tok_path))
TOK.no_truncation()


def ntok(text: str) -> int:
    return len(TOK.encode(text).ids)


docs = {}
for f in sorted(glob.glob(str(ROOT / "data/clean/*.json"))):
    d = json.loads(Path(f).read_text())
    docs[d["doc_id"]] = d

print(f"# Documents: {len(docs)}\n")

# ---- 1. Size per document and per type --------------------------------------------------------
by_type = collections.defaultdict(list)
rows = []
for doc_id, d in docs.items():
    text = "\n".join(p["text"] for p in d["pages"])
    lines = text.splitlines()
    t = ntok(text)
    rows.append((doc_id, d["source_type"], len(d["pages"]), len(text), len(text.split()), t, len(lines)))
    by_type[d["source_type"]].append(t)
print("## Size per document")
print(f"{'doc_id':32} {'type':15} {'pages':>5} {'words':>7} {'tokens':>7} {'lines':>6}")
for r in rows:
    print(f"{r[0]:32} {r[1]:15} {r[2]:5} {r[4]:7} {r[5]:7} {r[6]:6}")
total_tokens = sum(r[5] for r in rows)
print(f"\nTotal tokens: {total_tokens:,}  words: {sum(r[4] for r in rows):,}")
print("\n## Tokens by source type (sum / docs)")
for k, v in sorted(by_type.items(), key=lambda kv: -sum(kv[1])):
    print(f"{k:16} {sum(v):8,}  ({len(v)} docs, {100 * sum(v) / total_tokens:.0f}%)")

# ---- 2. Line structure --------------------------------------------------------------------------
print("\n## Line structure by type (median words/line, % short lines <=4 words, % ALL-CAPS heading-like)")
for typ in sorted(by_type):
    ls = [l for d in docs.values() if d["source_type"] == typ for p in d["pages"] for l in p["text"].splitlines()]
    words = [len(l.split()) for l in ls]
    short = sum(1 for w in words if w <= 4) / len(words)
    caps = sum(1 for l in ls if re.sub(r"[^A-Za-z]", "", l).isupper() and len(re.sub(r"[^A-Za-z]", "", l)) >= 3) / len(ls)
    print(f"{typ:16} median={st.median(words):4.0f}  short={100 * short:4.0f}%  caps={100 * caps:4.0f}%  lines={len(ls)}")


# ---- 3. Boilerplate: lines shared across same-type documents ----------------------------------
def norm(l):
    return re.sub(r"\s+", " ", re.sub(r"hdfc [a-z &\-]+? fund( of fund)?", "<scheme>", l.lower())).strip()


print("\n## Cross-document boilerplate (scheme names normalised)")
for typ in ["sid", "kim", "fund_facts"]:
    ids = [i for i, d in docs.items() if d["source_type"] == typ]
    if len(ids) < 2:
        continue
    sets = {i: {norm(l) for p in docs[i]["pages"] for l in p["text"].splitlines() if len(l.split()) >= 4} for i in ids}
    counts = collections.Counter(l for s in sets.values() for l in s)
    for i in ids[:1]:
        shared_all = sum(1 for l in sets[i] if counts[l] == len(ids))
        shared_most = sum(1 for l in sets[i] if counts[l] >= len(ids) - 1)
        print(f"{typ:10} ({len(ids)} docs) e.g. {i}: {100 * shared_all / len(sets[i]):.0f}% of lines identical in ALL docs, "
              f"{100 * shared_most / len(sets[i]):.0f}% in all-but-one")

# ---- 4. Per-page repeated lines (headers/footers) -------------------------------------------
print("\n## Repeated page headers/footers (lines on >=40% of a document's pages)")
for doc_id, d in docs.items():
    if len(d["pages"]) < 5:
        continue
    c = collections.Counter(l.strip() for p in d["pages"] for l in set(p["text"].splitlines()))
    rep = [(l, n) for l, n in c.items() if n >= 0.4 * len(d["pages"]) and len(l) > 3]
    if rep:
        print(f"{doc_id:32} {len(rep)} lines, e.g. {[l for l, _ in rep[:3]]}")

# ---- 5. Extraction artifacts ------------------------------------------------------------------
print("\n## Extraction artifacts")
art = collections.Counter()
examples = collections.defaultdict(list)
patterns = {
    "split_word (e.g. 'th e')": r"\b[a-z]{1,2} [a-z]{1,2}\b(?= [a-z])",
    "hyphen_linebreak": r"[a-z]-\n[a-z]",
    "glued_words (lowerUpper)": r"[a-z]{3,}[A-Z][a-z]{2,}",
    "missing_space_after_period": r"[a-z]\.[A-Z][a-z]",
    "empty_value ('of is')": r"\bof (is|are|shall)\b",
}
for doc_id, d in docs.items():
    text = "\n".join(p["text"] for p in d["pages"])
    for name, pat in patterns.items():
        found = re.findall(pat, text)
        art[(d["source_type"], name)] += len(found)
        if found and len(examples[name]) < 3:
            examples[name].append(f"{doc_id}: {found[0]!r}")
for (typ, name), n in sorted(art.items()):
    if n:
        print(f"{typ:16} {name:32} {n}")
for name, ex in examples.items():
    print(f"  e.g. {name}: {ex}")

# ---- 6. Current chunking: token lengths vs 512-token model limit ------------------------------
reg = load_registry()
names = {s.id: s.name.split(" – ")[0] for s in reg.schemes}
from ingest.chunk import scheme_name_index

idx = scheme_name_index({s.id: [s.name.split(" – ")[0], *s.aliases] for s in reg.schemes})
lens = []
for doc_id, d in docs.items():
    pd = ParsedDoc(kind=d["kind"], pages=tuple(Page(p["number"], p["text"]) for p in d["pages"]))
    src = next(s for s in reg.all_sources() if s.id == doc_id)
    chunks = chunk_document(pd, src.title, src.scheme_id, names, idx if src.multi_scheme else None)
    lens += [ntok(c.text) for c in chunks]
over = sum(1 for x in lens if x > 512)
print(f"\n## Current chunking (380 words / 40 overlap): {len(lens)} chunks")
print(f"tokens: median={st.median(lens):.0f}  p90={sorted(lens)[int(.9 * len(lens))]}  max={max(lens)}")
print(f"chunks > 512 tokens (truncated by bge/arctic models): {over} ({100 * over / len(lens):.0f}%)")

# ---- 7. Where gold facts live -------------------------------------------------------------------
gold = yaml.safe_load((ROOT / "tests/gold_facts.yaml").read_text())
print("\n## Gold facts: occurrences of the accepted value in the source document")
for key, g in gold.items():
    if not g.get("value"):
        continue
    d = docs.get(g["source_doc"])
    if not d:
        print(f"{key:40} source not ingested yet")
        continue
    lines = [l for p in d["pages"] for l in p["text"].splitlines()]
    hits = [i for i, l in enumerate(lines) if any(a.lower() in l.lower() for a in g["accept"])]
    print(f"{key:40} {g['source_doc']:28} lines matching={len(hits):4}")
