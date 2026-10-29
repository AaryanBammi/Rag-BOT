"""Smoke test for the external APIs: `python -m app.smoke`.

Makes one strict structured-output call to each configured Groq model and one embedding call
with the configured provider (local model or Voyage). Exits non-zero, naming what failed, if any check fails (edge case LLM-07).
"""

from __future__ import annotations

import sys

from app.config import ConfigError, settings
from app.llm import LLMError, complete_json

SMOKE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string", "enum": ["yes", "no"]},
    },
    "required": ["answer"],
    "additionalProperties": False,
}


def check_groq_model(model: str) -> dict:
    """One strict json_schema call; raises LLMError if the model can't do it."""
    result = complete_json(
        model,
        system="Reply with JSON only.",
        user="Is 2 greater than 1? Answer yes or no.",
        schema_name="SmokeCheck",
        schema=SMOKE_SCHEMA,
    )
    if result.get("answer") not in {"yes", "no"}:
        raise LLMError("bad_output", f"unexpected result {result!r}")
    return result


def check_embedder() -> int:
    """One query embedding with the configured provider; returns the vector size."""
    from ingest.embed import get_embedder

    return len(get_embedder().embed_query("Exit load of HDFC Mid Cap Fund"))


def main() -> int:
    failures = 0
    for role, model in (("classifier", settings.classifier_model), ("generator", settings.generator_model)):
        try:
            result = check_groq_model(model)
            print(f"OK    Groq {role:10} {model}  -> {result}")
        except (LLMError, ConfigError) as e:
            failures += 1
            print(f"FAIL  Groq {role:10} {model}  -> {e}")
    label = f"{settings.embedding_provider} embedding"
    try:
        dim = check_embedder()
        print(f"OK    {label:20} {settings.embedding_model}  -> {dim} dimensions")
    except Exception as e:  # noqa: BLE001 - report any embedding failure, don't crash
        failures += 1
        print(f"FAIL  {label:20} {settings.embedding_model}  -> {type(e).__name__}: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
