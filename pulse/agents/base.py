"""Provider-neutral backend contract and error types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BackendResult:
    data: dict[str, Any]
    input_tokens: int  # uncached input tokens
    output_tokens: int
    cache_read_tokens: int = 0
    reported_cost: float | None = None  # provider-reported USD (OpenRouter)


class Backend(Protocol):
    def complete(
        self, model: str, system: str, user: str, schema: dict, schema_name: str
    ) -> BackendResult: ...


class TransientError(Exception):
    """Retryable provider failure: rate limit, 5xx, connection error."""


class ProviderError(Exception):
    """Non-retryable provider failure: bad request, auth, unknown model."""


class OutputInvalid(Exception):
    """The model answered, but the output could not be parsed. Carries usage so it is billed."""

    def __init__(
        self,
        message: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        reported_cost: float | None = None,
    ):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.reported_cost = reported_cost


class BudgetExceeded(Exception):
    """The daily USD cap is reached; no call was made."""


class LLMError(Exception):
    """The call failed after retries; recorded in agent_runs as failed."""


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class StepResult:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    reported_cost: float | None = None


class ToolBackend(Protocol):
    """One model turn in a tool loop.

    transcript turns: {"role": "user", "content": str}
                      {"role": "assistant", "text": str | None, "tool_calls": list[ToolCall]}
                      {"role": "tool", "results": [{"id": str, "content": str}], "note": str (optional)}
    """

    def tool_step(
        self, model: str, system: str, transcript: list[dict], tools: list[ToolSpec], allow_tools: bool = True
    ) -> StepResult: ...


class ToolError(Exception):
    """A tool rejected its arguments. The message is returned to the model, not raised to the caller."""
