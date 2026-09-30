"""Shared test helpers. No network, no real providers."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pulse.config import AGENTS, Config, ModelRef, Price
from pulse.models import Message, to_iso

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def msg(
    id: str,
    content: str = "hello",
    *,
    minutes: float = 0,
    channel_id: str = "100",
    thread_id: str | None = None,
    author_id: str = "u1",
    author_name: str = "alice",
    reply_to_id: str | None = None,
    is_bot: bool = False,
) -> Message:
    return Message(
        id=id,
        guild_id="900",
        channel_id=channel_id,
        channel_name="help",
        author_id=author_id,
        author_name=author_name,
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
        thread_id=thread_id,
        reply_to_id=reply_to_id,
        is_bot=is_bot,
    )


def make_config(**overrides) -> Config:
    base = dict(
        guild_id="900",
        channel_ids=(),
        team_member_ids=frozenset({"t1"}),
        reply_window_hours=12.0,
        frustration_threshold=-2,
        models={a: ModelRef("anthropic", f"m-{a}") for a in AGENTS},
        daily_usd_cap=5.0,
        pricing={f"anthropic:m-{a}": Price(1.0, 5.0, 0.1) for a in AGENTS},
        launches=(),
        db_path=Path(":memory:"),
        imports_dir=Path("imports"),
    )
    base.update(overrides)
    return Config(**base)


def set_triage(conn, message_id: str, *, sentiment: int = 0, needs_reply: bool = False, kind: str = "question") -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO triage (message_id, sentiment, confidence, kind, topics,"
            " needs_reply, prompt_version, created_at) VALUES (?, ?, 0.9, ?, ?, ?, 'test', ?)",
            (message_id, sentiment, kind, json.dumps([]), int(needs_reply), to_iso(T0)),
        )


from pulse.agents.base import BackendResult  # noqa: E402
from pulse.agents.llm import LLMClient  # noqa: E402

FIXED_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class FakeBackend:
    """Replays queued responses, then falls back to handler(user) -> BackendResult.

    A queued item may be a BackendResult, an Exception (raised), or a callable
    taking the user prompt and returning either.
    """

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls: list[dict] = []

    def complete(self, model, system, user, schema, schema_name):
        self.calls.append({"model": model, "system": system, "user": user, "schema_name": schema_name})
        item = self.responses.pop(0) if self.responses else self.handler
        if item is None:
            raise AssertionError("FakeBackend has no response queued")
        if callable(item) and not isinstance(item, BackendResult):
            item = item(user)
        if isinstance(item, Exception):
            raise item
        return item


from pulse.agents.classifier import ClassifierResult  # noqa: E402
from pulse.config import ClassifierConfig  # noqa: E402


def jev_result(p=0.1, kind="other", sentiment=0, kind_conf=0.9, sent_conf=0.9, cost=0.00002) -> ClassifierResult:
    return ClassifierResult(
        needs_reply_p=p, kind=kind, kind_confidence=kind_conf, sentiment=sentiment,
        sentiment_confidence=sent_conf, reported_cost=cost,
    )


def classifier_config(**overrides) -> ClassifierConfig:
    base = dict(enabled=True, model=ModelRef("jev", "jev-latest"))
    base.update(overrides)
    return ClassifierConfig(**base)


class FakeClassifier:
    """Like FakeBackend: queued items first, then handler(state). Exceptions are raised."""

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls: list[dict] = []

    def classify(self, model, state):
        self.calls.append({"model": model, "state": state})
        item = self.responses.pop(0) if self.responses else self.handler
        if item is None:
            raise AssertionError("FakeClassifier has no response queued")
        if callable(item) and not isinstance(item, ClassifierResult):
            item = item(state)
        if isinstance(item, Exception):
            raise item
        return item


def make_llm(conn, config, backend, *, sleeps=None, classifier=None) -> LLMClient:
    sleeps = [] if sleeps is None else sleeps
    return LLMClient(
        conn, config, {"anthropic": backend}, now=lambda: FIXED_NOW, sleep=sleeps.append, classifier=classifier
    )
