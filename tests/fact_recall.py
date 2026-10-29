"""Chunk-level fact recall (Docs/eval.md Phase 1): `python -m tests.fact_recall [--data-dir PATH]`.

For every gold fact with a value, checks that at least one stored chunk from its source document
(and, for scheme facts, tagged with that scheme) contains an accepted form of the value, plus any
`must_also_contain` text. Also flags gold entries whose source document has changed since
labelling (stale) so they get re-checked rather than silently failing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import chromadb
import yaml

from app.config import PROJECT_ROOT, settings
from app.registry import load_registry
from ingest.manifest import Manifest
from ingest.store import COLLECTION

GOLD_PATH = PROJECT_ROOT / "tests" / "gold_facts.yaml"


def check(data_dir: Path) -> tuple[list[str], list[str], list[str]]:
    """Returns (passed, failed, stale) gold keys."""
    gold = yaml.safe_load(GOLD_PATH.read_text(encoding="utf-8"))
    col = chromadb.PersistentClient(path=str(data_dir / "index")).get_collection(COLLECTION, embedding_function=None)
    hashes = {row.doc_id: row.content_hash for row in Manifest(data_dir / "manifest.json").all()}
    general_ids = {s.id for s in load_registry().general}

    passed, failed, stale = [], [], []
    for key, fact in gold.items():
        if not fact.get("value"):
            continue
        doc = fact["source_doc"]
        if fact.get("doc_hash") and hashes.get(doc) not in (None, fact["doc_hash"]):
            stale.append(key)
        scheme = key.split(".")[0]
        # Scheme-specific facts must come from chunks tagged with that scheme, unless the source
        # is a general document (e.g. the SAI) whose chunks cover every scheme.
        general_doc = doc in general_ids
        where = {"doc_id": doc} if scheme == "general" or general_doc else {"$and": [{"doc_id": doc}, {"scheme_id": scheme}]}
        texts = [t.lower() for t in col.get(where=where, include=["documents"])["documents"]]
        accepts = [a.lower() for a in fact.get("chunk_accept") or fact["accept"]]
        extras = [e.lower() for e in fact.get("must_also_contain", [])]
        ok = any(any(a in t for a in accepts) and all(e in t for e in extras) for t in texts)
        (passed if ok else failed).append(key)
    return passed, failed, stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.fact_recall")
    parser.add_argument("--data-dir", type=Path, default=settings.data_dir)
    args = parser.parse_args(argv)
    passed, failed, stale = check(args.data_dir)
    for key in failed:
        print(f"MISSING  {key}")
    for key in stale:
        print(f"STALE    {key}  (source document changed since labelling; re-check the value)")
    total = len(passed) + len(failed)
    print(f"\nFact recall: {len(passed)}/{total} ({100 * len(passed) / max(total, 1):.0f}%)")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
