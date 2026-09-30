"""Idempotent persistence of ingested messages."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from pulse.models import Message, to_iso

_COLUMNS = (
    "guild_id", "channel_id", "channel_name", "thread_id", "author_id", "author_name",
    "author_avatar_url", "is_team", "is_bot", "content", "created_at", "edited_at",
    "reply_to_id", "source",
)
_INSERT = (
    f"INSERT INTO messages (id, {', '.join(_COLUMNS)}) "
    f"VALUES (?, {', '.join('?' for _ in _COLUMNS)})"
)
_UPDATE = f"UPDATE messages SET {', '.join(f'{c} = ?' for c in _COLUMNS)} WHERE id = ?"


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def _values(m: Message, team_ids: frozenset[str]) -> tuple:
    return (
        m.guild_id, m.channel_id, m.channel_name, m.thread_id, m.author_id, m.author_name,
        m.author_avatar_url, int(m.author_id in team_ids), int(m.is_bot), m.content,
        to_iso(m.created_at), to_iso(m.edited_at) if m.edited_at else None,
        m.reply_to_id, m.source,
    )


def upsert_messages(
    conn: sqlite3.Connection, messages: Iterable[Message], team_ids: frozenset[str]
) -> UpsertStats:
    stats = UpsertStats()
    with conn:
        for m in messages:
            existing = conn.execute("SELECT content FROM messages WHERE id = ?", (m.id,)).fetchone()
            values = _values(m, team_ids)
            if existing is None:
                conn.execute(_INSERT, (m.id, *values))
                stats.inserted += 1
                continue
            conn.execute(_UPDATE, (*values, m.id))
            if existing["content"] != m.content:
                # Edited message: its old labels no longer apply.
                conn.execute("DELETE FROM triage WHERE message_id = ?", (m.id,))
                stats.updated += 1
            else:
                stats.unchanged += 1
    return stats
