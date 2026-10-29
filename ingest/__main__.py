"""`python -m ingest` - run the ingestion pipeline and print a per-document summary.

Options:
  --only ID [ID ...]   ingest only these registry source ids (skips reference snapshots and
                       orphan cleanup)
  --rebuild            drop the vector index and re-embed everything (after changing the embedding
                       provider or model)
  --fake-embeddings    offline dry run with deterministic fake vectors; requires --data-dir so
                       fake vectors never mix with the real index
  --data-dir PATH      use a different data directory (default: DATA_DIR from config)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.config import ConfigError, settings
from app.registry import load_registry
from ingest.embed import FakeEmbedder, get_embedder
from ingest.manifest import Manifest
from ingest.pipeline import Ingestor, Outcome
from ingest.store import ChunkStore, StoreError

STATUS_ORDER = ["updated", "unchanged", "needs_manual", "failed", "removed", "reference", "reference_failed"]


def _print_outcome(o: Outcome) -> None:
    detail = f"{o.chunks:4} chunks  as_of={o.as_of or '-'}" if o.status in {"updated", "unchanged"} else ""
    print(f"  {o.status:16} {o.doc_id:30} {detail}{'  ' + o.note if o.note else ''}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest", description=__doc__.splitlines()[0])
    parser.add_argument("--only", nargs="+", metavar="ID")
    parser.add_argument("--rebuild", action="store_true")
    parser.add_argument("--fake-embeddings", action="store_true")
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)

    if args.fake_embeddings and not args.data_dir:
        parser.error("--fake-embeddings requires --data-dir (keeps fake vectors out of the real index)")
    data_dir = (args.data_dir or settings.data_dir).resolve()

    try:
        embedder = FakeEmbedder() if args.fake_embeddings else get_embedder()
        store_cls = ChunkStore.rebuild if args.rebuild else ChunkStore
        store = store_cls(data_dir / "index", embedder.model)
    except (ConfigError, StoreError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    registry = load_registry()
    manifest = Manifest(data_dir / "manifest.json")
    if args.rebuild:
        for row in manifest.all():
            manifest.delete(row.doc_id)

    print(f"Ingesting into {data_dir} with {embedder.model}\n")
    ingestor = Ingestor(
        registry, manifest, store, embedder, raw_dir=data_dir / "raw", clean_dir=data_dir / "clean"
    )
    summary = ingestor.run(only=set(args.only) if args.only else None, progress=_print_outcome)
    for o in summary.outcomes:
        if o.status == "removed":
            _print_outcome(o)

    counts = ", ".join(f"{summary.count(s)} {s}" for s in STATUS_ORDER if summary.count(s))
    print(f"\n{counts}. Total chunks in index: {store.count()}")
    if summary.count("needs_manual"):
        print("Some sources must be saved from a browser first; see the needs_manual lines above.")
    return 0 if summary.ok else 1


if __name__ == "__main__":
    sys.exit(main())
