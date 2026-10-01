"""Pain point status set by the community team (spec 15.2).

A status belongs to the active root theme. A status set on a theme that later merged
moves to the theme it merged into; when several apply, the newest update wins.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from pulse.models import from_iso, to_iso
from pulse.stats import theme_member_ids, theme_resolution

STATUSES = ("new", "acknowledged", "in_progress", "shipped")
LABELS = {
    "new": "Not triaged",
    "acknowledged": "Acknowledged",
    "in_progress": "Fix in progress",
    "shipped": "Fix shipped",
}
NOTE_MAX = 500
SHIP_WINDOW_DAYS = 14

_DEFAULT = {"status": "new", "label": LABELS["new"], "note": "", "updated_at": None, "shipped_at": None}


def statuses(conn: sqlite3.Connection) -> dict[int, dict]:
    resolved = theme_resolution(conn)
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT * FROM theme_status ORDER BY updated_at, theme_id"):
        root = resolved.get(r["theme_id"], r["theme_id"])
        out[root] = {
            "status": r["status"],
            "label": LABELS[r["status"]],
            "note": r["note"],
            "updated_at": r["updated_at"],
            "shipped_at": r["shipped_at"],
        }
    return out


def status_for(conn: sqlite3.Connection, theme_id: int) -> dict:
    root = theme_resolution(conn).get(theme_id, theme_id)
    return statuses(conn).get(root, dict(_DEFAULT))


def set_status(conn: sqlite3.Connection, theme_id: int, status: str, note: str, now: datetime) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    resolved = theme_resolution(conn)
    if theme_id not in resolved:
        raise LookupError(f"unknown theme {theme_id}")
    root = resolved[theme_id]
    current = status_for(conn, root)
    if status == "shipped":
        shipped_at = current["shipped_at"] if current["status"] == "shipped" and current["shipped_at"] else to_iso(now)
    else:
        shipped_at = None
    with conn:
        conn.execute(
            "INSERT INTO theme_status (theme_id, status, note, updated_at, shipped_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(theme_id) DO UPDATE SET status = excluded.status, note = excluded.note,"
            " updated_at = excluded.updated_at, shipped_at = excluded.shipped_at",
            (root, status, note.strip()[:NOTE_MAX], to_iso(now), shipped_at),
        )


def _window(conn: sqlite3.Connection, member_ids: list[int], start: datetime, end: datetime) -> dict:
    marks = ",".join("?" * len(member_ids))
    rows = conn.execute(
        "SELECT DISTINCT m.id, t.sentiment FROM message_themes mt JOIN messages m ON m.id = mt.message_id"
        " JOIN triage t ON t.message_id = m.id"
        f" WHERE mt.theme_id IN ({marks}) AND m.is_team = 0 AND m.is_bot = 0"
        " AND m.created_at >= ? AND m.created_at < ?",
        (*member_ids, to_iso(start), to_iso(end)),
    ).fetchall()
    n = len(rows)
    return {"messages": n, "avg_sentiment": round(sum(r["sentiment"] for r in rows) / n, 3) if n else None}


def shipped_comparison(conn: sqlite3.Connection, theme_id: int, now: datetime) -> dict | None:
    """Volume and mood in equal windows (up to 14 days) before and after the fix shipped."""
    current = status_for(conn, theme_id)
    if current["status"] != "shipped" or not current["shipped_at"]:
        return None
    shipped = from_iso(current["shipped_at"])
    after_end = max(shipped, min(shipped + timedelta(days=SHIP_WINDOW_DAYS), now))
    span = after_end - shipped
    ids = theme_member_ids(conn, theme_id)
    return {
        "shipped_at": current["shipped_at"],
        "days": round(span.total_seconds() / 86400, 1),
        "before": _window(conn, ids, shipped - span, shipped),
        "after": _window(conn, ids, shipped, after_end),
    }
