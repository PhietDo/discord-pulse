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
