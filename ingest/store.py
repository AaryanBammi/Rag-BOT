"""Chroma vector store for chunks (architecture.md §3.3).

Chunk ids are `<doc_id>:<version>:<index>`, where version is a prefix of the document's content
hash. New versions are upserted before old versions are deleted, so a failure part-way through
never leaves a document with no chunks (edge cases IDX-01, IDX-05).

Chroma metadata can't hold None, so a missing scheme_id, as_of, or page is stored as "" / 0.
"""

from __future__ import annotations

from pathlib import Path

import chromadb

COLLECTION = "chunks"


class StoreError(Exception):
    pass


class ChunkStore:
    def __init__(self, path: Path, embedding_model: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(path))
        self._col = self._client.get_or_create_collection(
            COLLECTION,
            embedding_function=None,
            metadata={"hnsw:space": "cosine", "embedding_model": embedding_model},
        )
        stored_model = (self._col.metadata or {}).get("embedding_model")
        if stored_model != embedding_model:
            # Vectors from different models can't be compared (IDX-07).
            raise StoreError(
                f"Vector store was built with embedding model {stored_model!r} but config uses "
                f"{embedding_model!r}. Re-run ingestion with --rebuild to re-embed everything."
            )

    @classmethod
    def rebuild(cls, path: Path, embedding_model: str) -> "ChunkStore":
        path.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(path))
        if COLLECTION in [c.name for c in client.list_collections()]:
            client.delete_collection(COLLECTION)
        return cls(path, embedding_model)

    def upsert(self, ids: list[str], embeddings: list[list[float]], documents: list[str], metadatas: list[dict]) -> None:
        if ids:
            self._col.upsert(ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas)

    def delete_other_versions(self, doc_id: str, keep_version: str) -> None:
        self._col.delete(where={"$and": [{"doc_id": doc_id}, {"version": {"$ne": keep_version}}]})

    def delete_doc(self, doc_id: str) -> None:
        self._col.delete(where={"doc_id": doc_id})

    def count(self, doc_id: str | None = None, version: str | None = None) -> int:
        if doc_id is None:
            return self._col.count()
        where: dict = {"doc_id": doc_id}
        if version is not None:
            where = {"$and": [where, {"version": version}]}
        return len(self._col.get(where=where, include=[])["ids"])

    def get(self, where: dict | None = None) -> list[dict]:
        """All chunks matching `where`, as dicts with id, text and metadata, in index order."""
        res = self._col.get(where=where, include=["documents", "metadatas"])
        rows = [
            {"id": i, "text": d, **m}
            for i, d, m in zip(res["ids"], res["documents"], res["metadatas"])
        ]
        return sorted(rows, key=lambda r: (r["doc_id"], r["chunk_index"]))
