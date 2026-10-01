"""Jev through OpenRouter's /systemone endpoint (TypeSafe closed-set classifier)."""
from __future__ import annotations

import os
from dataclasses import replace

import httpx

from pulse.agents.base import OutputInvalid, ProviderError, TransientError
from pulse.agents.classifier import JEV_QUESTIONS, ChoiceResult, ClassifierResult, parse_answers, parse_choice

SYSTEMONE_URL = "https://openrouter.ai/api/v1/systemone"


class JevBackend:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        api_key: str | None = None,
        url: str = SYSTEMONE_URL,
        timeout: float = 30.0,
    ):
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self._url = url

    def _post(self, model: str, state: dict, questions: dict) -> tuple[dict, float | None]:
        try:
            resp = self._client.post(
                self._url,
                json={"model": model, "state": state, "questions": questions},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TransportError as e:
            raise TransientError(str(e)) from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"{resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"{resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise OutputInvalid(f"not JSON: {e}") from e
        reported: float | None = None
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict) and usage.get("cost") is not None:
            try:
                reported = float(usage["cost"])
            except (TypeError, ValueError):
                reported = None
        return data, reported

    def classify(self, model: str, state: dict) -> ClassifierResult:
        data, reported = self._post(model, state, JEV_QUESTIONS)
        try:
            result = parse_answers(data)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise OutputInvalid(f"unexpected answers: {e!r}", reported_cost=reported) from e
        return replace(result, reported_cost=reported)

    def choose(self, model: str, state: dict, question: dict) -> ChoiceResult:
        data, reported = self._post(model, state, {"choice": question})
        try:
            result = parse_choice(data, question)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise OutputInvalid(f"unexpected choice: {e!r}", reported_cost=reported) from e
        return replace(result, reported_cost=reported)
