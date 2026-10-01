"""Rows for the shared message card (spec 5)."""
from __future__ import annotations

import sqlite3
from typing import Iterable

from pulse.links import jump_link

CARD_SQL = (
    "SELECT m.id AS message_id, m.guild_id, m.channel_id, m.channel_name, m.author_id, m.author_name,"
    " m.author_avatar_url, m.is_team, m.content, m.created_at, t.kind, t.sentiment"
    " FROM messages m LEFT JOIN triage t ON t.message_id = m.id"
)


def card(row: sqlite3.Row) -> dict:
    return {
        "message_id": row["message_id"],
        "link": jump_link(row["guild_id"], row["channel_id"], row["message_id"]),
        "author": row["author_name"],
        "author_id": row["author_id"],
        "avatar_url": row["author_avatar_url"],
        "is_team": bool(row["is_team"]),
        "channel": row["channel_name"] or row["channel_id"],
        "content": row["content"],
        "created_at": row["created_at"],
        "kind": row["kind"],
        "sentiment": row["sentiment"],
    }


def cards_by_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> list[dict]:
    """Cards in the order given; duplicates and unknown ids are skipped."""
    ordered = list(dict.fromkeys(ids))
    if not ordered:
        return []
    marks = ",".join("?" * len(ordered))
    rows = {r["message_id"]: r for r in conn.execute(f"{CARD_SQL} WHERE m.id IN ({marks})", ordered)}
    return [card(rows[i]) for i in ordered if i in rows]
