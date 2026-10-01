"""The single entry point agents use to call a model.

Resolves the agent's provider:model, enforces the daily budget, retries
transient errors with backoff, validates structured output (one retry),
and records exactly one agent_runs row per call.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, NoReturn

import jsonschema

from pulse.agents.base import (
    Backend, BackendResult, BudgetExceeded, LLMError, OutputInvalid, ProviderError, StepResult, ToolCall, ToolError,
    ToolSpec, TransientError,
)
from pulse.agents.classifier import Classifier, ChoiceResult, ClassifierResult
from pulse.config import Config, ModelRef, Price
from pulse.models import to_iso
from pulse.pricing import cost_usd, request_cost

MAX_TRANSIENT_ATTEMPTS = 3
MAX_VALIDATION_ATTEMPTS = 2


@dataclass(frozen=True)
class LLMResponse:
    data: dict[str, Any]
    run_id: int


@dataclass(frozen=True)
class ClassifyResponse:
    result: ClassifierResult
    run_id: int


@dataclass(frozen=True)
class ChoiceResponse:
    result: ChoiceResult
    run_id: int


@dataclass(frozen=True)
class ToolRunResponse:
    text: str
    run_id: int
    tool_calls: int


TOOL_LIMIT_NOTE = "Tool call limit reached. Write your final answer now from what you have."


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, price: Price | None, inp: int, out: int, cache: int, reported: float | None) -> None:
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += cache
        self.cost_usd += cost_usd(price, inp, out, cache, reported)


class LLMClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        backends: Mapping[str, Backend],
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        classifier: Classifier | None = None,
    ):
        self._conn = conn
        self._config = config
        self._backends = dict(backends)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._classifier = classifier
        # Serializes this client's DB access across worker threads.
        self._lock = threading.Lock()

    @property
    def db_lock(self) -> threading.Lock:
        """Read-only access to this client's DB lock, so callers can serialize their
        own writes to the same connection against this client's internal writes."""
        return self._lock

    @property
    def config(self) -> Config:
        return self._config

    @property
    def has_classifier(self) -> bool:
        return self._classifier is not None and self._config.classifier is not None

    def spent_today(self) -> float:
        midnight = self._now().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs WHERE started_at >= ?",
                (to_iso(midnight),),
            ).fetchone()
        return float(row[0])

    def complete(
        self,
        agent: str,
        system: str,
        user: str,
        schema: dict,
        schema_name: str,
        validate: Callable[[dict], object] | None = None,
    ) -> LLMResponse:
        """Call a model and return the validated response.

        Args:
            agent: Agent name (e.g., "triage").
            system: System prompt.
            user: User prompt.
            schema: JSON schema for structured output validation.
            schema_name: Schema name for logging.
            validate: Optional validation callback. Receives schema-valid data.
                Must raise ValueError to reject the output (triggering one retry).
                Any other exception is treated as a bug and propagates without
                recording an agent_runs row.

        Returns:
            LLMResponse with data and run_id.

        Raises:
            BudgetExceeded: Daily budget cap reached (no call made, run recorded as "skipped_budget").
            LLMError: Call failed after retries (run recorded as "failed" with error message).
            Any exception from validate() that is not ValueError.
        """
        ref = self._config.models[agent]
        price = self._config.pricing.get(str(ref))
        started = self._now()
        # Concurrent callers may each pass this check, so a run can overshoot
        # the cap by at most (concurrency - 1) calls.
        if self.spent_today() >= self._config.daily_usd_cap:
            self._record(agent, ref, started, "skipped_budget", "daily budget cap reached", _Usage())
            raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

        backend = self._backends[ref.provider]
        usage = _Usage()
        error = "no attempt made"
        for _ in range(MAX_VALIDATION_ATTEMPTS):
            try:
                result = self._call(backend, ref.model, system, user, schema, schema_name)
            except OutputInvalid as e:
                usage.add(price, e.input_tokens, e.output_tokens, e.cache_read_tokens, e.reported_cost)
                error = f"invalid output: {e}"
                continue
            except (TransientError, ProviderError) as e:
                error = f"provider error: {e}"
                break
            usage.add(price, result.input_tokens, result.output_tokens, result.cache_read_tokens, result.reported_cost)
            try:
                jsonschema.validate(result.data, schema)
                if validate is not None:
                    validate(result.data)
            except jsonschema.ValidationError as e:
                error = f"invalid output: {e.message}"
                continue
            except ValueError as e:
                error = f"invalid output: {e}"
                continue
            run_id = self._record(agent, ref, started, "ok", None, usage)
            return LLMResponse(result.data, run_id)

        self._record(agent, ref, started, "failed", error, usage)
        raise LLMError(f"{agent}: {error}")

    def _classifier_call(self, call: Callable[[Classifier, str], Any]) -> tuple[Any, int]:
        """Budget gate, transient backoff, one retry on invalid output, one agent_runs row."""
        cfg = self._config.classifier
        if cfg is None or self._classifier is None:
            raise RuntimeError("classifier not configured")
        ref = cfg.model
        price = self._config.pricing.get(str(ref))
        started = self._now()
        if self.spent_today() >= self._config.daily_usd_cap:
            self._record("classifier", ref, started, "skipped_budget", "daily budget cap reached", _Usage())
            raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

        usage = _Usage()
        error = "no attempt made"
        for _ in range(MAX_VALIDATION_ATTEMPTS):
            try:
                result = self._with_backoff(lambda: call(self._classifier, ref.model))
            except OutputInvalid as e:
                usage.cost_usd += request_cost(price, e.reported_cost)
                error = f"invalid output: {e}"
                continue
            except (TransientError, ProviderError) as e:
                error = f"provider error: {e}"
                break
            usage.cost_usd += request_cost(price, result.reported_cost)
            return result, self._record("classifier", ref, started, "ok", None, usage)

        self._record("classifier", ref, started, "failed", error, usage)
        raise LLMError(f"classifier: {error}")

    def classify(self, state: dict) -> ClassifyResponse:
        """Ask the configured classifier (Jev) about one message state.

        Same contract as complete(): budget gate, transient backoff, one retry on
        invalid output, exactly one agent_runs row (agent "classifier").
        """
        result, run_id = self._classifier_call(lambda c, model: c.classify(model, state))
        return ClassifyResponse(result, run_id)

    def choose(self, state: dict, question: dict) -> ChoiceResponse:
        """Ask the classifier (Jev) one choice question about one message state."""
        result, run_id = self._classifier_call(lambda c, model: c.choose(model, state, question))
        return ChoiceResponse(result, run_id)

    def run_tools(
        self,
        agent: str,
        system: str,
        user: str,
        tools: list[ToolSpec],
        execute: Callable[[ToolCall], str],
        *,
        max_calls: int = 12,
    ) -> ToolRunResponse:
        """Run a tool loop until the model answers without tools. One agent_runs row per run."""
        ref = self._config.models[agent]
        price = self._config.pricing.get(str(ref))
        started = self._now()
        if self.spent_today() >= self._config.daily_usd_cap:
            self._record(agent, ref, started, "skipped_budget", "daily budget cap reached", _Usage())
            raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

        backend = self._backends[ref.provider]
        transcript: list[dict] = [{"role": "user", "content": user}]
        usage = _Usage()
        calls = 0
        while True:
            allow = calls < max_calls
            try:
                step: StepResult = self._with_backoff(
                    lambda: backend.tool_step(ref.model, system, transcript, tools, allow)
                )
            except OutputInvalid as e:
                usage.add(price, e.input_tokens, e.output_tokens, e.cache_read_tokens, e.reported_cost)
                self._fail_run(agent, ref, started, usage, f"invalid output: {e}")
            except (TransientError, ProviderError) as e:
                self._fail_run(agent, ref, started, usage, f"provider error: {e}")
            usage.add(price, step.input_tokens, step.output_tokens, step.cache_read_tokens, step.reported_cost)

            if not step.tool_calls or not allow:
                text = (step.text or "").strip()
                if not text:
                    self._fail_run(agent, ref, started, usage, "empty final answer")
                return ToolRunResponse(text, self._record(agent, ref, started, "ok", None, usage), calls)

            if self.spent_today() + usage.cost_usd >= self._config.daily_usd_cap:
                self._record(agent, ref, started, "failed", "daily budget cap reached mid-run", usage)
                raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

            transcript.append({"role": "assistant", "text": step.text, "tool_calls": list(step.tool_calls)})
            results = []
            for call in step.tool_calls:
                calls += 1
                try:
                    content = execute(call)
                except ToolError as e:
                    content = f"error: {e}"
                results.append({"id": call.id, "content": content})
            turn: dict = {"role": "tool", "results": results}
            if calls >= max_calls:
                turn["note"] = TOOL_LIMIT_NOTE
            transcript.append(turn)

    def _fail_run(self, agent: str, ref: ModelRef, started: datetime, usage: _Usage, error: str) -> NoReturn:
        self._record(agent, ref, started, "failed", error, usage)
        raise LLMError(f"{agent}: {error}")

    def _with_backoff(self, fn: Callable[[], Any]) -> Any:
        for attempt in range(MAX_TRANSIENT_ATTEMPTS):
            try:
                return fn()
            except TransientError:
                if attempt == MAX_TRANSIENT_ATTEMPTS - 1:
                    raise
                self._sleep(2**attempt)
        raise AssertionError("unreachable")

    def _call(self, backend: Backend, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        return self._with_backoff(lambda: backend.complete(model, system, user, schema, schema_name))

    def _record(self, agent: str, ref: ModelRef, started: datetime, status: str, error: str | None, usage: _Usage) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO agent_runs (agent, model, input_tokens, output_tokens, cache_read_tokens,"
                " cost_usd, status, error, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent, str(ref), usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                    usage.cost_usd, status, error, to_iso(started), to_iso(self._now()),
                ),
            )
            return int(cur.lastrowid)
