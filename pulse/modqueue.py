"""Deterministic mod queue: who needs a human reply."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from pulse.config import Config
from pulse.models import to_iso
from pulse.stats import first_team_reply, scope_clause

# Keeps the first import of months of history from flooding the queue.
QUEUE_LOOKBACK_DAYS = 7


@dataclass
class ModQueueStats:
    opened: int = 0
    updated: int = 0
    auto_closed: int = 0


def _team_replied(conn: sqlite3.Connection, message_id: str, thread_id: str | None, created_at: str) -> bool:
    # A message with no thread_id can still be the message a Discord thread was
    # started from (thread id == starter message id), so also match replies
    # whose thread_id is this message's own id.
    return first_team_reply(conn, message_id, thread_id, created_at) is not None


def list_open(
    conn: sqlite3.Connection, limit: int = 20, *, channels: tuple[str, ...] | None = None
) -> list[sqlite3.Row]:
    """Open items, highest priority first: frustrated, then Jev's needs_reply
    probability (an LLM-only row counts as its 0/1 label), then oldest."""
    scope, params = scope_clause(channels)
    return conn.execute(
        "SELECT q.id AS queue_id, q.reason, m.id AS message_id, m.guild_id, m.channel_id, m.channel_name,"
        " m.thread_id, m.author_name, m.content, m.created_at, t.needs_reply_p"
        " FROM mod_queue q JOIN messages m ON m.id = q.message_id"
        " LEFT JOIN triage t ON t.message_id = m.id"
        f" WHERE q.status = 'open'{scope}"
        " ORDER BY (q.reason = 'frustrated') DESC, COALESCE(t.needs_reply_p, t.needs_reply, 0) DESC,"
        " m.created_at ASC, m.id ASC"
        " LIMIT ?",
        (*params, limit),
    ).fetchall()


def refresh_mod_queue(conn: sqlite3.Connection, config: Config, now: datetime) -> ModQueueStats:
    stats = ModQueueStats()
    now_iso = to_iso(now)
    reply_cutoff = to_iso(now - timedelta(hours=config.reply_window_hours))
    lookback = to_iso(now - timedelta(days=QUEUE_LOOKBACK_DAYS))

    with conn:
        open_unanswered = conn.execute(
            "SELECT q.id, m.id AS message_id, m.thread_id, m.created_at FROM mod_queue q"
            " JOIN messages m ON m.id = q.message_id WHERE q.status = 'open' AND q.reason = 'unanswered'"
        ).fetchall()
        for item in open_unanswered:
            if _team_replied(conn, item["message_id"], item["thread_id"], item["created_at"]):
                conn.execute(
                    "UPDATE mod_queue SET status = 'handled', closed_at = ?, closed_by = 'auto' WHERE id = ?",
                    (now_iso, item["id"]),
                )
                stats.auto_closed += 1

        triggers = conn.execute(
            "SELECT m.id, m.thread_id, m.created_at, t.sentiment, t.needs_reply FROM messages m"
            " JOIN triage t ON t.message_id = m.id"
            " WHERE m.is_team = 0 AND m.is_bot = 0 AND m.created_at >= ?"
            " AND (t.sentiment <= ? OR (t.needs_reply = 1 AND m.created_at <= ?))"
            " ORDER BY m.created_at, m.id",
            (lookback, config.frustration_threshold, reply_cutoff),
        ).fetchall()

        for m in triggers:
            reason = "frustrated" if m["sentiment"] <= config.frustration_threshold else "unanswered"
            if reason == "unanswered" and _team_replied(conn, m["id"], m["thread_id"], m["created_at"]):
                continue
            if conn.execute("SELECT 1 FROM mod_queue WHERE message_id = ?", (m["id"],)).fetchone():
                continue
            key = m["thread_id"] or m["id"]
            open_item = conn.execute(
                "SELECT q.id, q.reason, m.created_at FROM mod_queue q JOIN messages m ON m.id = q.message_id"
                " WHERE q.queue_key = ? AND q.status = 'open'",
                (key,),
            ).fetchone()
            if open_item is not None:
                if m["created_at"] > open_item["created_at"]:
                    new_reason = "frustrated" if "frustrated" in (reason, open_item["reason"]) else "unanswered"
                    conn.execute(
                        "UPDATE mod_queue SET message_id = ?, reason = ? WHERE id = ?",
                        (m["id"], new_reason, open_item["id"]),
                    )
                    stats.updated += 1
                continue
            last_closed = conn.execute(
                "SELECT MAX(closed_at) FROM mod_queue WHERE queue_key = ? AND status != 'open'", (key,)
            ).fetchone()[0]
            if last_closed is not None and m["created_at"] <= last_closed:
                continue
            conn.execute(
                "INSERT INTO mod_queue (queue_key, message_id, thread_id, reason, status, opened_at)"
                " VALUES (?, ?, ?, ?, 'open', ?)",
                (key, m["id"], m["thread_id"], reason, now_iso),
            )
            stats.opened += 1
    return stats
