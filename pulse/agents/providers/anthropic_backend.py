"""Anthropic backend: structured output via a single forced tool, and tool-loop steps."""
from __future__ import annotations

from typing import Any

import anthropic

from pulse.agents.base import (
    BackendResult, OutputInvalid, ProviderError, StepResult, ToolCall, ToolSpec, TransientError,
)


def _usage(resp) -> tuple[int, int, int]:
    usage = resp.usage
    # Cache writes are billed at the input rate (slight undercount of the write premium).
    input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", None) or 0)
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    return input_tokens, usage.output_tokens or 0, cache_read


def _anthropic_messages(transcript: list[dict]) -> list[dict]:
    out = []
    last_tool = max((i for i, t in enumerate(transcript) if t["role"] == "tool"), default=None)
    for i, turn in enumerate(transcript):
        role = turn["role"]
        if role == "user":
            out.append({"role": "user", "content": turn["content"]})
        elif role == "assistant":
            content: list[dict] = []
            if turn.get("text"):
                content.append({"type": "text", "text": turn["text"]})
            content += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in turn.get("tool_calls", [])
            ]
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            content = [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in turn["results"]
            ]
            if i == last_tool and content:
                # Cache the transcript prefix up to the newest tool output: the next step re-sends it all.
                content[-1]["cache_control"] = {"type": "ephemeral"}
            if turn.get("note"):
                content.append({"type": "text", "text": turn["note"]})
            out.append({"role": "user", "content": content})
        else:
            raise ValueError(f"unknown transcript role {role!r}")
    return out


class AnthropicBackend:
    def __init__(self, client=None, *, max_tokens: int = 8192):
        self._client = client if client is not None else anthropic.Anthropic()
        self._max_tokens = max_tokens

    def _system(self, system: str) -> list[dict]:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _send(self, kwargs: dict[str, Any]):
        try:
            return self._client.messages.create(**kwargs)
        except anthropic.APIConnectionError as e:
            raise TransientError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise ProviderError(str(e)) from e

    def complete(self, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        resp = self._send(dict(
            model=model,
            max_tokens=self._max_tokens,
            system=self._system(system),
            messages=[{"role": "user", "content": user}],
            tools=[{"name": schema_name, "description": "Record the structured result.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": schema_name},
        ))
        input_tokens, output_tokens, cache_read = _usage(resp)
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == schema_name), None)
        if block is None:
            raise OutputInvalid("no tool_use block in response", input_tokens, output_tokens, cache_read)
        return BackendResult(dict(block.input), input_tokens, output_tokens, cache_read)

    def tool_step(
        self, model: str, system: str, transcript: list[dict], tools: list[ToolSpec], allow_tools: bool = True
    ) -> StepResult:
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=self._max_tokens, system=self._system(system),
            messages=_anthropic_messages(transcript),
        )
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
            kwargs["tool_choice"] = {"type": "auto"} if allow_tools else {"type": "none"}
        resp = self._send(kwargs)
        input_tokens, output_tokens, cache_read = _usage(resp)
        text = "\n".join(b.text for b in resp.content if b.type == "text") or None
        calls = tuple(ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use")
        return StepResult(text, calls, input_tokens, output_tokens, cache_read)
