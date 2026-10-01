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


from pulse.agents.base import StepResult, ToolCall, ToolSpec

TOOLS = [ToolSpec("search_messages", "Find messages.", {"type": "object", "properties": {"text": {"type": "string"}}})]
TRANSCRIPT = [
    {"role": "user", "content": "why?"},
    {"role": "assistant", "text": "looking", "tool_calls": [ToolCall("tu1", "search_messages", {"text": "auth"})]},
    {"role": "tool", "results": [{"id": "tu1", "content": "[3 messages]"}], "note": "answer now"},
]


def test_tool_step_translates_transcript_and_parses_tool_use():
    resp = response([
        SimpleNamespace(type="text", text="Let me check threads."),
        SimpleNamespace(type="tool_use", id="tu2", name="search_messages", input={"text": "token"}),
    ])
    backend, messages = backend_for(resp)
    result = backend.tool_step("claude-sonnet-5", "sys", TRANSCRIPT, TOOLS)
    assert result == StepResult("Let me check threads.", (ToolCall("tu2", "search_messages", {"text": "token"}),), 150, 20, 900)
    kw = messages.kwargs
    assert kw["tools"] == [{"name": "search_messages", "description": "Find messages.", "input_schema": TOOLS[0].parameters}]
    assert kw["tool_choice"] == {"type": "auto"}
    assert kw["messages"] == [
        {"role": "user", "content": "why?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "tu1", "name": "search_messages", "input": {"text": "auth"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "[3 messages]",
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "answer now"},
        ]},
    ]


def test_tool_step_final_answer_with_tools_disabled():
    backend, messages = backend_for(response([SimpleNamespace(type="text", text="Final.")]))
    result = backend.tool_step("m", "sys", TRANSCRIPT[:1], TOOLS, allow_tools=False)
    assert (result.text, result.tool_calls) == ("Final.", ())
    assert messages.kwargs["tool_choice"] == {"type": "none"}


def test_tool_step_maps_errors_like_complete():
    err = anthropic.RateLimitError("slow", response=httpx.Response(429, request=REQ), body=None)
    with pytest.raises(TransientError):
        backend_for(err)[0].tool_step("m", "sys", TRANSCRIPT[:1], TOOLS)


def test_tool_step_caches_only_the_last_result_of_the_latest_tool_turn():
    transcript = [
        *TRANSCRIPT,
        {"role": "assistant", "text": None, "tool_calls": [
            ToolCall("tu2", "search_messages", {"text": "a"}), ToolCall("tu3", "search_messages", {"text": "b"}),
        ]},
        {"role": "tool", "results": [{"id": "tu2", "content": "A"}, {"id": "tu3", "content": "B"}]},
    ]
    backend, messages = backend_for(response([SimpleNamespace(type="text", text="ok")]))
    backend.tool_step("m", "sys", transcript, TOOLS)
    sent = messages.kwargs["messages"]
    assert "cache_control" not in sent[2]["content"][0]
    assert sent[4]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu2", "content": "A"},
        {"type": "tool_result", "tool_use_id": "tu3", "content": "B", "cache_control": {"type": "ephemeral"}},
    ]
    assert messages.kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in transcript[-1]["results"][-1]


def test_tool_step_joins_text_blocks_with_newlines():
    backend, _ = backend_for(response([
        SimpleNamespace(type="text", text="First."), SimpleNamespace(type="text", text="Second."),
    ]))
    assert backend.tool_step("m", "sys", TRANSCRIPT[:1], TOOLS).text == "First.\nSecond."
