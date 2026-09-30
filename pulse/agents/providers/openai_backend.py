"""OpenAI Chat Completions backend. Also serves OpenRouter via base_url."""
from __future__ import annotations

import json
import os
from typing import Any

import openai

from pulse.agents.base import BackendResult, OutputInvalid, ProviderError, TransientError

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_CONSTRAINT_KEYWORDS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "format",
})


def strict_schema(schema: dict) -> dict:
    """Copy of schema without constraint keywords strict mode may reject.

    LLMClient validates the full schema locally, so nothing is lost.
    """

    def strip(node: Any, is_properties_map: bool = False) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if not is_properties_map and key in _CONSTRAINT_KEYWORDS:
                    continue
                out[key] = strip(value, is_properties_map=(key == "properties" and not is_properties_map))
            return out
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(schema)


class OpenAIBackend:
    def __init__(self, client=None, *, openrouter: bool = False):
        self._openrouter = openrouter
        if client is None:
            client = (
                openai.OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
                if openrouter
                else openai.OpenAI()
            )
        self._client = client

    def complete(self, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": strict_schema(schema), "strict": True},
        }
        try:
            resp = self._create(model, system, user, response_format)
        except ProviderError:
            if not self._openrouter:
                raise
            # Some OpenRouter models reject json_schema; fall back to JSON mode.
            fallback_system = (
                f"{system}\n\nRespond with only a JSON object matching this JSON Schema:\n{json.dumps(schema)}"
            )
            resp = self._create(model, fallback_system, user, {"type": "json_object"})
        return self._parse(resp)

    def _create(self, model: str, system: str, user: str, response_format: dict):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": response_format,
        }
        if self._openrouter:
            kwargs["extra_body"] = {"usage": {"include": True}}
        try:
            return self._client.chat.completions.create(**kwargs)
        except openai.APIConnectionError as e:
            raise TransientError(str(e)) from e
        except openai.APIStatusError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise ProviderError(str(e)) from e

    def _parse(self, resp) -> BackendResult:
        usage = resp.usage
        details = getattr(usage, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", None) or 0) if details is not None else 0
        input_tokens = (usage.prompt_tokens or 0) - cached
        output_tokens = usage.completion_tokens or 0
        cost = getattr(usage, "cost", None)
        reported = float(cost) if cost is not None else None

        def invalid(reason: str) -> OutputInvalid:
            return OutputInvalid(reason, input_tokens, output_tokens, cached, reported)

        message = resp.choices[0].message
        if getattr(message, "refusal", None):
            raise invalid(f"model refused: {message.refusal}")
        try:
            data = json.loads(message.content or "")
        except json.JSONDecodeError as e:
            raise invalid(f"not JSON: {e}") from e
        if not isinstance(data, dict):
            raise invalid("expected a JSON object")
        return BackendResult(data, input_tokens, output_tokens, cached, reported)
