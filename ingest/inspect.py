"""`python -m ingest.inspect <scheme_id|doc_id|general>` - list stored chunks to eyeball parsing.

Options:
  --grep TEXT      only chunks containing TEXT (case-insensitive)
  --full           print whole chunks instead of the first 300 characters
  --data-dir PATH  inspect a different data directory
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import chromadb

from app.config import settings
from ingest.store import COLLECTION


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.inspect")
    parser.add_argument("target", help="scheme id (all its chunks), doc id, or 'general'")
    parser.add_argument("--grep")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)

    path = (args.data_dir or settings.data_dir) / "index"
    client = chromadb.PersistentClient(path=str(path))
    try:
        col = client.get_collection(COLLECTION, embedding_function=None)
    except Exception:  # noqa: BLE001
        print(f"No vector index at {path}. Run `python -m ingest` first.", file=sys.stderr)
        return 1

    where = (
        {"scheme_id": ""} if args.target == "general"
        else {"$or": [{"scheme_id": args.target}, {"doc_id": args.target}]}
    )
    res = col.get(where=where, include=["documents", "metadatas"])
    rows = sorted(zip(res["documents"], res["metadatas"]), key=lambda r: (r[1]["doc_id"], r[1]["chunk_index"]))
    if args.grep:
        rows = [r for r in rows if args.grep.lower() in r[0].lower()]

    for text, meta in rows:
        page = f" p{meta['page']}" if meta.get("page") else ""
        print(f"--- {meta['doc_id']} #{meta['chunk_index']}{page}  [{meta['source_type']}]  as_of={meta['as_of'] or '-'}")
        print(text if args.full else text[:300] + ("…" if len(text) > 300 else ""))
        print()
    print(f"{len(rows)} chunk(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
