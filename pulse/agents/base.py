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
