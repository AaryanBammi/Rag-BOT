"""Thin wrapper around Groq for strict JSON-schema calls (architecture.md §8).

Every model call in the app goes through `complete_json` / `acomplete_json`:
non-streaming, temperature 0, strict `json_schema` response format. All Groq failures are
mapped to one `LLMError` with a `kind`, so callers can return the "Service unavailable"
template without knowing Groq's exception types (edge cases LLM-01 – LLM-03, LLM-08).

Two opt-in hooks for the eval runner (both off in the app): `enable_cache(dir)` stores each
response on disk keyed by the full request, so re-running unchanged questions costs no tokens;
`on_call` receives one event per call (model, tokens used, cached, error), for pacing under
Groq's per-minute limit and for reporting token use.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import groq

from app.config import settings

DEFAULT_MAX_COMPLETION_TOKENS = 2048  # includes the model's reasoning tokens
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2  # SDK retries 408/409/429/5xx and connection errors


class LLMError(Exception):
    """A model call failed. `kind` is one of:

    rate_limited  Groq returned 429 after retries
    unavailable   5xx or connection failure after retries
    timeout       request timed out after retries
    bad_output    response was cut off, empty, or not valid JSON
    bad_request   request rejected (e.g. model doesn't support strict json_schema, bad key)
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind = kind


def _response_format(schema_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {"name": schema_name, "strict": True, "schema": schema},
    }


def _request(
    model: str,
    system: str,
    user: str,
    schema_name: str,
    schema: dict[str, Any],
    reasoning_effort: str | None,
    max_completion_tokens: int,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "response_format": _response_format(schema_name, schema),
        "temperature": 0,
        "max_completion_tokens": max_completion_tokens,
        "stream": False,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
        kwargs["include_reasoning"] = False
    return kwargs


def _parse(response: Any) -> dict[str, Any]:
    if not response.choices:
        raise LLMError("bad_output", "response has no choices")
    choice = response.choices[0]
    if choice.finish_reason != "stop":
        raise LLMError("bad_output", f"finish_reason={choice.finish_reason!r}")
    content = choice.message.content
    if not content:
        raise LLMError("bad_output", "empty content")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        raise LLMError("bad_output", f"invalid JSON: {e}") from None
    if not isinstance(parsed, dict):
        raise LLMError("bad_output", "top-level JSON value is not an object")
    return parsed


def _map_error(e: Exception) -> LLMError:
    # Order matters: specific subclasses before their bases.
    if isinstance(e, groq.RateLimitError):
        return LLMError("rate_limited", str(e))
    if isinstance(e, groq.APITimeoutError):
        return LLMError("timeout", str(e))
    if isinstance(e, groq.InternalServerError):
        return LLMError("unavailable", str(e))
    if isinstance(e, groq.APIConnectionError):
        return LLMError("unavailable", str(e))
    if isinstance(e, groq.APIStatusError):
        if e.status_code >= 500:
            return LLMError("unavailable", str(e))
        return LLMError("bad_request", str(e))
    return LLMError("unavailable", f"{type(e).__name__}: {e}")


# --- eval hooks -----------------------------------------------------------------------------

on_call: Callable[[dict[str, Any]], None] | None = None
_cache_dir: Path | None = None


def enable_cache(directory: Path | None) -> None:
    """Cache responses in `directory` (None turns caching off)."""
    global _cache_dir
    _cache_dir = directory
    if directory:
        directory.mkdir(parents=True, exist_ok=True)


def _cache_path(kwargs: dict[str, Any]) -> Path | None:
    if _cache_dir is None:
        return None
    key = hashlib.sha256(json.dumps(kwargs, sort_keys=True).encode()).hexdigest()
    return _cache_dir / f"{key}.json"


def _cached(kwargs: dict[str, Any]) -> dict[str, Any] | None:
    path = _cache_path(kwargs)
    if path and path.exists():
        _emit(kwargs["model"], 0, cached=True)
        return json.loads(path.read_text())
    return None


def _store(kwargs: dict[str, Any], parsed: dict[str, Any]) -> None:
    path = _cache_path(kwargs)
    if path:
        path.write_text(json.dumps(parsed))


def _emit(model: str, tokens: int, cached: bool = False, error: LLMError | None = None) -> None:
    if on_call:
        on_call({"model": model, "tokens": tokens, "cached": cached,
                 "error": error.kind if error else None, "message": str(error) if error else None})


def _finish(kwargs: dict[str, Any], response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    _emit(kwargs["model"], getattr(usage, "total_tokens", 0) or 0)
    parsed = _parse(response)
    _store(kwargs, parsed)
    return parsed


def _failed(kwargs: dict[str, Any], e: Exception) -> LLMError:
    error = _map_error(e)
    _emit(kwargs["model"], 0, error=error)
    return error


_sync_client: groq.Groq | None = None
_async_client: groq.AsyncGroq | None = None


def _client() -> groq.Groq:
    global _sync_client
    if _sync_client is None:
        settings.require("groq_api_key")
        _sync_client = groq.Groq(
            api_key=settings.groq_api_key,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            max_retries=DEFAULT_MAX_RETRIES,
        )
    return _sync_client


def _aclient() -> groq.AsyncGroq:
    global _async_client
    if _async_client is None:
        settings.require("groq_api_key")
        _async_client = groq.AsyncGroq(
            api_key=settings.groq_api_key,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            max_retries=DEFAULT_MAX_RETRIES,
        )
    return _async_client


def complete_json(
    model: str,
    system: str,
    user: str,
    schema_name: str,
    schema: dict[str, Any],
    *,
    reasoning_effort: str | None = "low",
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
) -> dict[str, Any]:
    """Call `model` and return its schema-valid JSON object, or raise LLMError."""
    kwargs = _request(model, system, user, schema_name, schema, reasoning_effort, max_completion_tokens)
    if (hit := _cached(kwargs)) is not None:
        return hit
    try:
        response = _client().chat.completions.create(**kwargs)
    except groq.GroqError as e:
        raise _failed(kwargs, e) from None
    return _finish(kwargs, response)


async def acomplete_json(
    model: str,
    system: str,
    user: str,
    schema_name: str,
    schema: dict[str, Any],
    *,
    reasoning_effort: str | None = "low",
    max_completion_tokens: int = DEFAULT_MAX_COMPLETION_TOKENS,
) -> dict[str, Any]:
    """Async version of `complete_json`, for the API server (Phase 4)."""
    kwargs = _request(model, system, user, schema_name, schema, reasoning_effort, max_completion_tokens)
    if (hit := _cached(kwargs)) is not None:
        return hit
    try:
        response = await _aclient().chat.completions.create(**kwargs)
    except groq.GroqError as e:
        raise _failed(kwargs, e) from None
    return _finish(kwargs, response)
