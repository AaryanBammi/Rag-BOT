"""data/manifest.json: one record per ingested document, plus Groww reference snapshots.

Replaces a database for document metadata (architecture.md §3.3): the corpus is small, and a
JSON file is readable, diffable, and easy to inspect. Every write goes to a temporary file
that is then renamed over the old one, so a crash never leaves a half-written manifest.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class DocumentRow:
    doc_id: str  # registry source id, stable when monthly URLs change
    url: str
    source_type: str
    scheme_id: str | None  # None for general sources
    title: str
    content_hash: str  # SHA-256 of the parsed text
    fetched_at: str  # ISO 8601 UTC
    as_of: str | None  # ISO date stated in the document
    chunk_count: int
    embedding_model: str
    updated_at: str  # when content last changed (ISO 8601 UTC)
    chunker_version: str = "1"  # ingest.chunk.CHUNKER_VERSION used to build the chunks


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Manifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = {}
        self._documents: dict[str, dict] = data.get("documents", {})
        self._references: dict[str, dict] = data.get("references", {})

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": _now(),
            "documents": dict(sorted(self._documents.items())),
            "references": dict(sorted(self._references.items())),
        }
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".manifest-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
                f.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    # -- documents ----------------------------------------------------------------------------

    def get(self, doc_id: str) -> DocumentRow | None:
        row = self._documents.get(doc_id)
        return DocumentRow(**row) if row else None

    def all(self) -> list[DocumentRow]:
        return [DocumentRow(**r) for _, r in sorted(self._documents.items())]

    def upsert(self, row: DocumentRow) -> None:
        self._documents[row.doc_id] = asdict(row)
        self._save()

    def touch(self, doc_id: str, fetched_at: str, url: str) -> None:
        """Record a fetch that found no content change."""
        self._documents[doc_id].update(fetched_at=fetched_at, url=url)
        self._save()

    def delete(self, doc_id: str) -> None:
        if self._documents.pop(doc_id, None) is not None:
            self._save()

    # -- Groww reference snapshots (product context only; never parsed, indexed, or cited) -----

    def set_reference(self, scheme_id: str, url: str, file: str, fetched_at: str) -> None:
        self._references[scheme_id] = {"url": url, "file": file, "fetched_at": fetched_at}
        self._save()

    def references(self) -> dict[str, dict]:
        return dict(self._references)
