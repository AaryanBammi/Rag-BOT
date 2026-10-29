"""The ingestion run: registry -> fetch -> parse -> hash -> as_of -> chunk -> embed -> store.

data/ layout:
  raw/       downloaded files (<source id>.pdf|.html), manual saves (raw/manual/), and Groww
             reference snapshots (<scheme id>-groww.html; never parsed, indexed, or cited)
  clean/     parsed text per document (<source id>.json), the input to chunking
  index/     Chroma vector store
  manifest.json  one record per ingested document + reference snapshots

Order of writes for a changed document (decision D-14):
  1. embed the new chunks          (failure: nothing written)
  2. upsert them into Chroma       (failure: old chunks still there)
  3. delete the old version        (failure: both versions present; next run cleans up)
  4. update the manifest last      (failure: next run sees the old hash and redoes 1-3)
So any failure leaves the previous version answerable, and re-running is always safe.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.registry import Registry, Source
from ingest import fetch as fetching
from ingest.chunk import CHUNKER_VERSION, chunk_document, scheme_name_index
from ingest.dates import extract_as_of
from ingest.embed import Embedder
from ingest.factcards import build_fact_cards
from ingest.manifest import DocumentRow, Manifest
from ingest.parse import ParsedDoc, ParseError, parse
from ingest.store import ChunkStore

POLITE_DELAY_SECONDS = 1.0  # between downloads from the same sites


@dataclass
class Outcome:
    doc_id: str
    status: str  # updated | unchanged | failed | needs_manual | removed | reference | reference_failed
    chunks: int = 0
    as_of: str | None = None
    note: str = ""


@dataclass
class RunSummary:
    outcomes: list[Outcome] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def ok(self) -> bool:
        return self.count("failed") == 0


def content_hash(text: str) -> str:
    """Hash of the parsed text, not raw bytes, so cosmetic HTML changes don't count (D-15)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def display_name(scheme_name: str) -> str:
    """'HDFC Mid Cap Fund – Direct Growth' -> 'HDFC Mid Cap Fund'."""
    return scheme_name.split(" – ")[0].strip()


class Ingestor:
    def __init__(
        self,
        registry: Registry,
        manifest: Manifest,
        store: ChunkStore,
        embedder: Embedder,
        raw_dir: Path,
        clean_dir: Path,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self.registry = registry
        self.manifest = manifest
        self.store = store
        self.embedder = embedder
        self.raw_dir = raw_dir
        self.clean_dir = clean_dir
        self.client = client
        self.sleep = sleep
        self.scheme_names = {s.id: display_name(s.name) for s in registry.schemes}
        self.multi_index = scheme_name_index(
            {s.id: [display_name(s.name), *s.aliases] for s in registry.schemes}
        )

    # -- per source ---------------------------------------------------------------------------

    def _http(self) -> httpx.Client:
        if self.client is None:
            self.client = fetching.http_client()
        return self.client

    def _write_clean(self, source: Source, doc: ParsedDoc, digest: str, as_of: str | None) -> None:
        record = {
            "doc_id": source.id,
            "title": source.title,
            "url": source.url,
            "source_type": source.type,
            "scheme_id": source.scheme_id,
            "kind": doc.kind,
            "content_hash": digest,
            "as_of": as_of,
            "pages": [{"number": p.number, "text": p.text} for p in doc.pages],
        }
        self.clean_dir.mkdir(parents=True, exist_ok=True)
        (self.clean_dir / f"{source.id}.json").write_text(
            json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def _get_raw(self, source: Source) -> fetching.Fetched:
        if source.fetch == "manual":
            return fetching.fetch_manual(source, self.raw_dir)
        fetched = fetching.fetch_auto(source, self._http(), sleep=self.sleep)
        ext = ".pdf" if fetched.raw[:4] == b"%PDF" else ".html"
        (self.raw_dir / f"{source.id}{ext}").write_bytes(fetched.raw)
        self.sleep(POLITE_DELAY_SECONDS)
        return fetched

    def ingest_source(self, source: Source) -> Outcome:
        try:
            fetched = self._get_raw(source)
        except fetching.ManualFileMissing as e:
            return Outcome(source.id, "needs_manual", note=str(e))
        except fetching.FetchError as e:
            return Outcome(source.id, "failed", note=f"fetch: {e}")

        try:
            doc = parse(fetched.raw)
        except ParseError as e:
            return Outcome(source.id, "failed", note=f"parse: {e}")

        digest = content_hash(doc.text)
        version = digest[:12]
        existing = self.manifest.get(source.id)
        if (
            existing
            and existing.content_hash == digest
            and existing.embedding_model == self.embedder.model
            and existing.chunker_version == CHUNKER_VERSION
            and self.store.count(source.id, version) == existing.chunk_count > 0
        ):
            self.manifest.touch(source.id, fetched.fetched_at, source.url)
            self.store.delete_other_versions(source.id, version)  # clears leftovers from a crashed run
            if not (self.clean_dir / f"{source.id}.json").exists():
                self._write_clean(source, doc, digest, existing.as_of)
            return Outcome(source.id, "unchanged", chunks=existing.chunk_count, as_of=existing.as_of)

        as_of = extract_as_of(doc.text, source.type)
        chunks = chunk_document(
            doc,
            title=source.title,
            scheme_id=source.scheme_id,
            scheme_names=self.scheme_names,
            multi_scheme_index=self.multi_index if source.multi_scheme else None,
            count_tokens=self.embedder.count_tokens,
            max_tokens=self.embedder.max_tokens,
        )
        if source.type == "factsheet" and source.multi_scheme:
            chunks += build_fact_cards(doc, source.title, self.multi_index, self.scheme_names, len(chunks))
        if not chunks:
            return Outcome(source.id, "failed", note="no chunks produced (no matching scheme sections?)")

        try:
            vectors = self.embedder.embed_documents([c.text for c in chunks])
        except Exception as e:  # noqa: BLE001 - any embedding failure keeps the old version
            return Outcome(source.id, "failed", note=f"embed: {type(e).__name__}: {e}")

        as_of_str = as_of.isoformat() if as_of else None
        self._write_clean(source, doc, digest, as_of_str)
        self.store.upsert(
            ids=[f"{source.id}:{version}:{c.index}" for c in chunks],
            embeddings=vectors,
            documents=[c.text for c in chunks],
            metadatas=[
                {
                    "doc_id": source.id,
                    "version": version,
                    "chunk_index": c.index,
                    "kind": c.kind,
                    "scheme_id": c.scheme_id or "",
                    "source_type": source.type,
                    "url": source.url,
                    "title": source.title,
                    "as_of": as_of_str or "",
                    "fetched_at": fetched.fetched_at,
                    "page": c.page or 0,
                }
                for c in chunks
            ],
        )
        self.store.delete_other_versions(source.id, version)
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        self.manifest.upsert(
            DocumentRow(
                doc_id=source.id,
                url=source.url,
                source_type=source.type,
                scheme_id=source.scheme_id,
                title=source.title,
                content_hash=digest,
                fetched_at=fetched.fetched_at,
                as_of=as_of_str,
                chunk_count=len(chunks),
                embedding_model=self.embedder.model,
                updated_at=now,
                chunker_version=CHUNKER_VERSION,
            )
        )
        return Outcome(source.id, "updated", chunks=len(chunks), as_of=as_of_str)

    # -- whole run ----------------------------------------------------------------------------

    def remove_orphans(self, keep_ids: set[str]) -> list[Outcome]:
        """Delete documents that are no longer in the registry (IDX-03)."""
        removed = []
        for row in self.manifest.all():
            if row.doc_id not in keep_ids:
                self.store.delete_doc(row.doc_id)
                self.manifest.delete(row.doc_id)
                (self.clean_dir / f"{row.doc_id}.json").unlink(missing_ok=True)
                removed.append(Outcome(row.doc_id, "removed", note="no longer in sources.yaml"))
        return removed

    def save_references(self, progress=None) -> list[Outcome]:
        """Snapshot each scheme's Groww page into raw/ for product context (decision 1).

        These files are never parsed, chunked, embedded, or cited. A failure here doesn't fail
        the run, because nothing the assistant answers depends on them.
        """
        outcomes = []
        for scheme in self.registry.schemes:
            ref_id = f"{scheme.id}-groww"
            try:
                fetched = fetching.fetch_reference(scheme.groww_reference, self._http())
            except fetching.FetchError as e:
                outcome = Outcome(ref_id, "reference_failed", note=str(e))
            else:
                (self.raw_dir / f"{ref_id}.html").write_bytes(fetched.raw)
                self.manifest.set_reference(scheme.id, scheme.groww_reference, f"raw/{ref_id}.html", fetched.fetched_at)
                outcome = Outcome(ref_id, "reference", note="context only, not indexed")
                self.sleep(POLITE_DELAY_SECONDS)
            outcomes.append(outcome)
            if progress:
                progress(outcome)
        return outcomes

    def run(self, only: set[str] | None = None, progress=None) -> RunSummary:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        sources = [s for s in self.registry.all_sources() if s.fetch != "none"]
        summary = RunSummary()
        for source in sources:
            if only and source.id not in only:
                continue
            outcome = self.ingest_source(source)
            summary.outcomes.append(outcome)
            if progress:
                progress(outcome)
        if not only:
            summary.outcomes.extend(self.save_references(progress))
            summary.outcomes.extend(self.remove_orphans({s.id for s in sources}))
        return summary
