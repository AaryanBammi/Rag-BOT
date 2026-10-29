"""Embedders (architecture.md §8).

- LocalEmbedder (default): BAAI/bge-small-en-v1.5 via fastembed (ONNX, runs on this machine,
  no API key). The model downloads once into .cache/fastembed/.
- VoyageEmbedder: Voyage AI API, used when EMBEDDING_PROVIDER=voyage.
- FakeEmbedder: deterministic vectors for offline tests.

Documents and queries are embedded differently (bge prepends a retrieval instruction to
queries; Voyage takes input_type), so each embedder has embed_documents and embed_query.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path
from typing import Protocol

from app.config import MODELS_CACHE_DIR, settings
from ingest.chunk import approx_tokens

VOYAGE_BATCH_SIZE = 32
VOYAGE_MAX_RETRIES = 5  # client-side retries with backoff, covers rate limits (IDX-06)
LOCAL_BATCH_SIZE = 32


class Embedder(Protocol):
    model: str
    max_tokens: int  # chunk size cap, below the model's input limit

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...

    def count_tokens(self, text: str) -> int: ...


def _find_tokenizer(model: str) -> Path | None:
    """The tokenizer.json fastembed downloaded for `model`, if present in the cache."""
    short = model.split("/")[-1].lower()
    for path in MODELS_CACHE_DIR.rglob("tokenizer.json"):
        if short in str(path).lower():
            return path
    return None


class LocalEmbedder:
    max_tokens = 480  # bge-small reads 512 tokens including special tokens

    def __init__(self, model: str | None = None) -> None:
        from fastembed import TextEmbedding

        self.model = model or settings.embedding_model
        MODELS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(model_name=self.model, cache_dir=str(MODELS_CACHE_DIR))
        self._tokenizer = None
        path = _find_tokenizer(self.model)
        if path is not None:
            from tokenizers import Tokenizer

            self._tokenizer = Tokenizer.from_file(str(path))
            self._tokenizer.no_truncation()

    def count_tokens(self, text: str) -> int:
        if self._tokenizer is None:
            return approx_tokens(text)
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self._model.passage_embed(texts, batch_size=LOCAL_BATCH_SIZE)]

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self._model.query_embed(text))).tolist()


class VoyageEmbedder:
    # voyage-4 reads 32k tokens; the word target, not this cap, sets chunk size.
    max_tokens = 4000

    def count_tokens(self, text: str) -> int:
        return approx_tokens(text)

    def __init__(self, model: str | None = None) -> None:
        import voyageai

        settings.require("voyage_api_key")
        self.model = model or settings.embedding_model
        self._client = voyageai.Client(api_key=settings.voyage_api_key, max_retries=VOYAGE_MAX_RETRIES)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), VOYAGE_BATCH_SIZE):
            batch = texts[start : start + VOYAGE_BATCH_SIZE]
            result = self._client.embed(batch, model=self.model, input_type="document")
            vectors.extend(result.embeddings)
        return vectors

    def embed_query(self, text: str) -> list[float]:
        return self._client.embed([text], model=self.model, input_type="query").embeddings[0]


def get_embedder() -> Embedder:
    """The embedder selected by EMBEDDING_PROVIDER."""
    if settings.embedding_provider == "voyage":
        return VoyageEmbedder()
    return LocalEmbedder()


class FakeEmbedder:
    """Deterministic bag-of-words vectors: similar texts get similar vectors. Tests only."""

    model = "fake-embedder"
    dims = 64
    max_tokens = 480

    def __init__(self) -> None:
        self.calls = 0

    def count_tokens(self, text: str) -> int:
        return approx_tokens(text)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        v = [0.0] * self.dims
        for word in text.lower().split():
            v[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dims] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]
