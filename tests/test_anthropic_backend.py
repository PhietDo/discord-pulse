from types import SimpleNamespace

import anthropic
import httpx
import pytest

from pulse.agents.base import OutputInvalid, ProviderError, TransientError
from pulse.agents.providers.anthropic_backend import AnthropicBackend

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}


class FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def backend_for(outcome):
    messages = FakeMessages(outcome)
    return AnthropicBackend(client=SimpleNamespace(messages=messages)), messages


def response(content, cache_read=900, cache_write=50):
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
        ),
    )


def test_returns_forced_tool_input_and_normalized_usage():
    resp = response([
        SimpleNamespace(type="text", text="ok"),
        SimpleNamespace(type="tool_use", name="answer_result", input={"answer": "x"}),
    ])
    backend, messages = backend_for(resp)
    result = backend.complete("claude-haiku-4-5-20251001", "sys", "hi", SCHEMA, "answer_result")
    assert result.data == {"answer": "x"}
    assert (result.input_tokens, result.output_tokens, result.cache_read_tokens) == (150, 20, 900)
    assert result.reported_cost is None
    kw = messages.kwargs
    assert kw["model"] == "claude-haiku-4-5-20251001"
    assert kw["tool_choice"] == {"type": "tool", "name": "answer_result"}
    assert kw["tools"][0]["input_schema"] == SCHEMA
    assert kw["system"][0] == {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    assert kw["messages"] == [{"role": "user", "content": "hi"}]


def test_missing_cache_fields_count_as_zero():
    resp = response([SimpleNamespace(type="tool_use", name="answer_result", input={"answer": "x"})], None, None)
    result = backend_for(resp)[0].complete("m", "sys", "hi", SCHEMA, "answer_result")
    assert (result.input_tokens, result.cache_read_tokens) == (100, 0)


def test_no_tool_block_is_output_invalid_with_usage():
    backend, _ = backend_for(response([SimpleNamespace(type="text", text="sorry")]))
    with pytest.raises(OutputInvalid) as exc:
        backend.complete("m", "sys", "hi", SCHEMA, "answer_result")
    assert exc.value.output_tokens == 20


def test_rate_limit_is_transient():
    err = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=REQ), body=None)
    with pytest.raises(TransientError):
        backend_for(err)[0].complete("m", "sys", "hi", SCHEMA, "answer_result")


def test_server_error_is_transient():
    err = anthropic.InternalServerError("overloaded", response=httpx.Response(529, request=REQ), body=None)
    with pytest.raises(TransientError):
        backend_for(err)[0].complete("m", "sys", "hi", SCHEMA, "answer_result")


def test_connection_error_is_transient():
    with pytest.raises(TransientError):
        backend_for(anthropic.APIConnectionError(request=REQ))[0].complete("m", "sys", "hi", SCHEMA, "answer_result")


def test_auth_error_is_provider_error():
    err = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=REQ), body=None)
    with pytest.raises(ProviderError):
        backend_for(err)[0].complete("m", "sys", "hi", SCHEMA, "answer_result")
