import copy
from types import SimpleNamespace

import httpx
import openai
import pytest

from pulse.agents.base import OutputInvalid, ProviderError, TransientError
from pulse.agents.providers.openai_backend import OpenAIBackend, strict_schema

REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "score", "tags", "pattern"],
    "properties": {
        "answer": {"type": "string"},
        "score": {"type": "integer", "minimum": -2, "maximum": 2},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "pattern": {"type": "string"},
    },
}
DATA = '{"answer": "x", "score": 1, "tags": [], "pattern": "p"}'


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def backend_for(*outcomes, openrouter=False):
    completions = FakeCompletions(outcomes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAIBackend(client=client, openrouter=openrouter), completions


def response(content=DATA, refusal=None, cost=None, cached=800):
    usage = SimpleNamespace(
        prompt_tokens=1000, completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    if cost is not None:
        usage.cost = cost
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=refusal))],
        usage=usage,
    )


def status_error(cls, code):
    return cls("err", response=httpx.Response(code, request=REQ), body=None)


def test_openai_structured_output_and_usage():
    backend, completions = backend_for(response())
    result = backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")
    assert result.data["answer"] == "x"
    assert (result.input_tokens, result.cache_read_tokens, result.output_tokens) == (200, 800, 50)
    assert result.reported_cost is None
    kw = completions.calls[0]
    assert kw["model"] == "gpt-x"
    assert kw["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    fmt = kw["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "answer_result"
    assert fmt["json_schema"]["strict"] is True
    assert "extra_body" not in kw


def test_strict_schema_strips_constraints_but_keeps_property_names():
    original = copy.deepcopy(SCHEMA)
    stripped = strict_schema(SCHEMA)
    assert SCHEMA == original
    assert "minimum" not in stripped["properties"]["score"]
    assert "maxItems" not in stripped["properties"]["tags"]
    assert "pattern" in stripped["properties"]
    assert stripped["required"] == SCHEMA["required"]


def test_missing_usage_details_count_as_zero_cached():
    resp = response()
    resp.usage.prompt_tokens_details = None
    result = backend_for(resp)[0].complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")
    assert (result.input_tokens, result.cache_read_tokens) == (1000, 0)


def test_openrouter_reports_cost_and_requests_usage():
    backend, completions = backend_for(response(cost=0.0042), openrouter=True)
    result = backend.complete("anthropic/claude-sonnet-5", "sys", "hi", SCHEMA, "answer_result")
    assert result.reported_cost == pytest.approx(0.0042)
    assert completions.calls[0]["extra_body"] == {"usage": {"include": True}}


def test_openrouter_falls_back_to_json_mode_on_bad_request():
    backend, completions = backend_for(
        status_error(openai.BadRequestError, 400), response(), openrouter=True
    )
    result = backend.complete("some/model", "sys", "hi", SCHEMA, "answer_result")
    assert result.data["answer"] == "x"
    fallback = completions.calls[1]
    assert fallback["response_format"] == {"type": "json_object"}
    assert '"answer"' in fallback["messages"][0]["content"]


def test_openai_bad_request_is_provider_error():
    backend, _ = backend_for(status_error(openai.BadRequestError, 400))
    with pytest.raises(ProviderError):
        backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")


def test_rate_limit_and_connection_errors_are_transient():
    with pytest.raises(TransientError):
        backend_for(status_error(openai.RateLimitError, 429))[0].complete("gpt-x", "s", "u", SCHEMA, "n")
    with pytest.raises(TransientError):
        backend_for(openai.APIConnectionError(request=REQ))[0].complete("gpt-x", "s", "u", SCHEMA, "n")


def test_non_json_content_is_output_invalid_with_usage():
    backend, _ = backend_for(response(content="not json"))
    with pytest.raises(OutputInvalid) as exc:
        backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")
    assert exc.value.output_tokens == 50


def test_refusal_is_output_invalid():
    backend, _ = backend_for(response(content=None, refusal="I can't help"))
    with pytest.raises(OutputInvalid, match="refused"):
        backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")


def test_json_array_is_output_invalid():
    backend, _ = backend_for(response(content="[1, 2]"))
    with pytest.raises(OutputInvalid):
        backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")
