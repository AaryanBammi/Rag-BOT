"""Offline tests for app/llm.py with a fake Groq client."""

from types import SimpleNamespace

import groq
import httpx
import pytest

from app import llm
from app.llm import LLMError

SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


def _response(content='{"ok": true}', finish_reason="stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=finish_reason, message=SimpleNamespace(content=content))]
    )


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def fake(monkeypatch):
    def install(result):
        client = FakeClient(result)
        monkeypatch.setattr(llm, "_client", lambda: client)
        return client

    return install


def _status_error(cls, status):
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return cls("boom", response=httpx.Response(status, request=request), body=None)


def test_returns_parsed_json_and_sends_strict_schema(fake):
    client = fake(_response())
    assert llm.complete_json("m", "sys", "user", "Check", SCHEMA) == {"ok": True}
    sent = client.calls[0]
    assert sent["temperature"] == 0 and sent["stream"] is False
    assert sent["response_format"]["json_schema"] == {"name": "Check", "strict": True, "schema": SCHEMA}
    assert sent["reasoning_effort"] == "low" and sent["include_reasoning"] is False


def test_reasoning_params_omitted_when_disabled(fake):
    client = fake(_response())
    llm.complete_json("m", "s", "u", "Check", SCHEMA, reasoning_effort=None)
    assert "reasoning_effort" not in client.calls[0] and "include_reasoning" not in client.calls[0]


@pytest.mark.parametrize(
    "response",
    [_response(finish_reason="length"), _response(content=""), _response(content="not json"), _response(content="[1]")],
)
def test_bad_output(fake, response):
    fake(response)
    with pytest.raises(LLMError) as e:
        llm.complete_json("m", "s", "u", "Check", SCHEMA)
    assert e.value.kind == "bad_output"


@pytest.mark.parametrize(
    "error,kind",
    [
        (_status_error(groq.RateLimitError, 429), "rate_limited"),
        (_status_error(groq.InternalServerError, 503), "unavailable"),
        (_status_error(groq.BadRequestError, 400), "bad_request"),
        (_status_error(groq.AuthenticationError, 401), "bad_request"),
        (groq.APITimeoutError(request=httpx.Request("POST", "https://api.groq.com")), "timeout"),
        (groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com")), "unavailable"),
    ],
)
def test_error_mapping(fake, error, kind):
    fake(error)
    with pytest.raises(LLMError) as e:
        llm.complete_json("m", "s", "u", "Check", SCHEMA)
    assert e.value.kind == kind


# --- eval hooks: response cache and per-call events -----------------------------------------

@pytest.fixture
def hooks(monkeypatch):
    events = []
    monkeypatch.setattr(llm, "on_call", events.append)
    yield events
    llm.enable_cache(None)


def test_cache_serves_repeat_requests_without_calling_groq(fake, hooks, tmp_path):
    llm.enable_cache(tmp_path)
    response = _response()
    response.usage = SimpleNamespace(total_tokens=321)
    client = fake(response)
    assert llm.complete_json("m", "sys", "user", "Check", SCHEMA) == {"ok": True}
    assert llm.complete_json("m", "sys", "user", "Check", SCHEMA) == {"ok": True}
    assert len(client.calls) == 1
    assert [(e["tokens"], e["cached"]) for e in hooks] == [(321, False), (0, True)]
    llm.complete_json("m", "sys", "other user", "Check", SCHEMA)  # different request: a real call
    assert len(client.calls) == 2


def test_cache_is_off_by_default_and_failures_are_not_cached(fake, hooks, tmp_path):
    client = fake(_response())
    llm.complete_json("m", "sys", "user", "Check", SCHEMA)
    llm.complete_json("m", "sys", "user", "Check", SCHEMA)
    assert len(client.calls) == 2  # no cache enabled
    llm.enable_cache(tmp_path)
    fake(_response(finish_reason="length"))
    with pytest.raises(LLMError):
        llm.complete_json("m", "sys", "u2", "Check", SCHEMA)
    assert list(tmp_path.iterdir()) == []


def test_errors_are_reported_to_the_hook(fake, hooks):
    fake(_status_error(groq.RateLimitError, 429))
    with pytest.raises(LLMError):
        llm.complete_json("m", "sys", "user", "Check", SCHEMA)
    assert hooks[-1]["error"] == "rate_limited" and hooks[-1]["model"] == "m"
