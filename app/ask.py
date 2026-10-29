"""Answer path for factual questions: retrieve -> conflict rule -> generate -> validate -> format.

    python -m app.ask "What is the exit load of HDFC Mid Cap Fund?"            # full pipeline
    python -m app.ask "What is the exit load?" --scheme hdfc-mid-cap           # skip the classifier

The full pipeline (app/pipeline.py) puts the PII guard and classifier in front of `Answerer.ask`.
"""

from __future__ import annotations

import argparse
import asyncio

from app.config import settings
from app.conflicts import prefer_latest
from app.generator import agenerate, generate
from app.registry import Registry, load_registry
from app.retriever import Retriever
from app.templates import ADVISORY_ASIDE, Templates
from app.validator import Reply, check, format_reply, source_date
from ingest.manifest import Manifest

__all__ = ["Answerer", "Reply"]


class Answerer:
    def __init__(self, retriever: Retriever | None = None, registry: Registry | None = None,
                 manifest: Manifest | None = None) -> None:
        # `is None`, not `or`: an empty retriever or manifest is falsy but still the one to use.
        self.retriever = retriever if retriever is not None else Retriever()
        self.registry = registry if registry is not None else load_registry()
        self.manifest = manifest if manifest is not None else Manifest(settings.manifest_path)
        self.templates = Templates(self.registry, self.manifest)

    def _scheme_name(self, scheme_id: str | None) -> str | None:
        """Current name plus registered former names, so a question using an old name still matches."""
        if not scheme_id:
            return None
        scheme = self.registry.scheme(scheme_id)
        name = scheme.name.split(" – ")[0]
        others = [a for a in scheme.aliases if a.lower() != name.lower()]
        return f"{name} (also called: {', '.join(others)})" if others else name

    def not_found(self, scheme_id: str | None, top_similarity: float | None = None) -> Reply:
        return self.templates.not_found(scheme_id, top_similarity)

    def ask(self, question: str, scheme_id: str | None = None, advisory_aside: bool = False) -> Reply:
        """Answer from the corpus, or fall back to a template. At most 2 generator calls."""
        chunks = self.retriever.search(question, scheme_id)
        early = self._before_generation(chunks, scheme_id)
        if early:
            return early
        chunks = prefer_latest(question, chunks)
        max_sentences = 2 if advisory_aside else 3
        reminder, outcome = None, None
        for attempt in range(2):
            generated = generate(question, chunks, self._scheme_name(scheme_id),
                                 max_sentences=max_sentences, reminder=reminder)
            outcome, reminder = self._after_generation(generated, chunks, scheme_id, advisory_aside, attempt)
            if outcome:
                return outcome
        raise AssertionError("unreachable: the second attempt always returns")

    async def aask(self, question: str, scheme_id: str | None = None, advisory_aside: bool = False) -> Reply:
        """Async `ask` for the API: retrieval runs in a worker thread, the model call is awaited."""
        chunks = await asyncio.to_thread(self.retriever.search, question, scheme_id)
        early = self._before_generation(chunks, scheme_id)
        if early:
            return early
        chunks = prefer_latest(question, chunks)
        max_sentences = 2 if advisory_aside else 3
        reminder = None
        for attempt in range(2):
            generated = await agenerate(question, chunks, self._scheme_name(scheme_id),
                                        max_sentences=max_sentences, reminder=reminder)
            outcome, reminder = self._after_generation(generated, chunks, scheme_id, advisory_aside, attempt)
            if outcome:
                return outcome
        raise AssertionError("unreachable: the second attempt always returns")

    def _before_generation(self, chunks: list, scheme_id: str | None) -> Reply | None:
        top = max((c.similarity for c in chunks), default=None)
        if not chunks or top < settings.min_similarity:
            return self.not_found(scheme_id, top)
        return None

    def _after_generation(self, generated, chunks, scheme_id, advisory_aside, attempt) -> tuple[Reply | None, str | None]:
        """(final reply, None) when done; (None, reminder) to regenerate once."""
        verdict = check(generated, chunks, 2 if advisory_aside else 3)
        top = max(c.similarity for c in chunks)
        if not verdict.ok:
            if verdict.retry and attempt == 0:
                return None, verdict.problem
            if verdict.fallback == "performance":
                return self.templates.performance(scheme_id), None
            if verdict.fallback == "advisory":
                return self.templates.advisory(), None
            return self.not_found(scheme_id, top), None
        cited = verdict.cited
        body = f"{verdict.answer} {ADVISORY_ASIDE}" if advisory_aside else verdict.answer
        footer = source_date(cited.as_of, cited.fetched_at)
        reply = Reply("answer", format_reply(body, cited.url, footer), cited.url, footer, cited=cited,
                      generated=generated, top_similarity=top, intent="factual", scheme_id=scheme_id)
        return reply, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ask")
    parser.add_argument("question")
    parser.add_argument("--scheme", help="registry scheme id; skips the PII guard and classifier")
    parser.add_argument("--debug", action="store_true", help="also show the route and cited chunk")
    args = parser.parse_args(argv)
    if args.scheme:
        reply = Answerer().ask(args.question, args.scheme)
    else:
        from app.pipeline import Pipeline

        reply = Pipeline().handle(args.question)
    print(reply.text)
    if args.debug:
        print(f"\n[{reply.kind}; intent={reply.intent} scheme={reply.scheme_id}]")
        if reply.cited:
            print(f"[cited {reply.cited.chunk_id} ({reply.cited.source_type}/{reply.cited.kind}), "
                  f"top similarity {reply.top_similarity:.3f}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
