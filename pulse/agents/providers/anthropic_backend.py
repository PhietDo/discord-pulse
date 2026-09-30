"""Anthropic backend: structured output via a single forced tool."""
from __future__ import annotations

import anthropic

from pulse.agents.base import BackendResult, OutputInvalid, ProviderError, TransientError


class AnthropicBackend:
    def __init__(self, client=None, *, max_tokens: int = 8192):
        self._client = client if client is not None else anthropic.Anthropic()
        self._max_tokens = max_tokens

    def complete(self, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                tools=[{"name": schema_name, "description": "Record the structured result.", "input_schema": schema}],
                tool_choice={"type": "tool", "name": schema_name},
            )
        except anthropic.APIConnectionError as e:
            raise TransientError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise ProviderError(str(e)) from e

        usage = resp.usage
        # Cache writes are billed at the input rate (slight undercount of the write premium).
        input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", None) or 0)
        cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
        output_tokens = usage.output_tokens or 0

        block = next((b for b in resp.content if b.type == "tool_use" and b.name == schema_name), None)
        if block is None:
            raise OutputInvalid("no tool_use block in response", input_tokens, output_tokens, cache_read)
        return BackendResult(dict(block.input), input_tokens, output_tokens, cache_read)
