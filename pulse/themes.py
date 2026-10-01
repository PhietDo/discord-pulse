"""Theme store: create, assign, merge, rename, with an append-only event log.

History is never rewritten: a merge marks the source theme merged and points it
at the target; reads resolve merged themes to their active root.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from pulse.models import to_iso
from pulse.stats import theme_resolution

MAX_NEW_THEMES_PER_RUN = 5
MAX_MERGES_PER_RUN = 3


def _clean(name: str) -> str:
    return " ".join(name.split())


def _event(conn: sqlite3.Connection, kind: str, payload: dict, run_id: int | None, now: datetime) -> None:
    conn.execute(
        "INSERT INTO theme_events (kind, payload, run_id, created_at) VALUES (?, ?, ?, ?)",
        (kind, json.dumps(payload), run_id, to_iso(now)),
    )


def active_themes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, name, description FROM themes WHERE status = 'active' ORDER BY id").fetchall()


def find_active(conn: sqlite3.Connection, name: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM themes WHERE status = 'active' AND lower(name) = lower(?)", (_clean(name),)
    ).fetchone()
    return row["id"] if row else None


def create_theme(
    conn: sqlite3.Connection, name: str, description: str, now: datetime, run_id: int | None = None
) -> tuple[int, bool]:
    name = _clean(name)
    if not name:
        raise ValueError("theme name is empty")
    existing = find_active(conn, name)
    if existing is not None:
        return existing, False
    description = description.strip()
    tid = int(conn.execute(
        "INSERT INTO themes (name, description, status, created_at) VALUES (?, ?, 'active', ?)",
        (name, description, to_iso(now)),
    ).lastrowid)
    _event(conn, "create", {"theme_id": tid, "name": name, "description": description}, run_id, now)
    return tid, True


def assign(conn: sqlite3.Connection, message_id: str, theme_id: int) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO message_themes (message_id, theme_id) VALUES (?, ?)", (message_id, theme_id)
    )
    return cur.rowcount == 1


def merge_themes(
    conn: sqlite3.Connection, from_id: int, into_id: int, now: datetime, run_id: int | None = None, reason: str = ""
) -> None:
    conn.execute("UPDATE themes SET status = 'merged', merged_into = ? WHERE id = ?", (into_id, from_id))
    _event(conn, "merge", {"from_id": from_id, "into_id": into_id, "reason": reason}, run_id, now)


def rename_theme(
    conn: sqlite3.Connection, theme_id: int, name: str, description: str, now: datetime, run_id: int | None = None
) -> None:
    old = conn.execute("SELECT name, description FROM themes WHERE id = ?", (theme_id,)).fetchone()
    name, description = _clean(name), description.strip()
    conn.execute("UPDATE themes SET name = ?, description = ? WHERE id = ?", (name, description, theme_id))
    _event(conn, "rename", {
        "theme_id": theme_id, "old_name": old["name"], "name": name,
        "old_description": old["description"], "description": description,
    }, run_id, now)


def mark_themed(conn: sqlite3.Connection, message_ids: Iterable[str], now: datetime) -> None:
    conn.executemany(
        "UPDATE triage SET themed_at = ? WHERE message_id = ?", [(to_iso(now), mid) for mid in message_ids]
    )


@dataclass
class ThemeBudget:
    new_themes_left: int = MAX_NEW_THEMES_PER_RUN
    merges_left: int = MAX_MERGES_PER_RUN


def budget_for_day(conn: sqlite3.Connection, now: datetime) -> ThemeBudget:
    """The guardrail is per UTC day: subtract creates and merges already logged since midnight."""
    midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    counts = dict(conn.execute(
        "SELECT kind, COUNT(*) FROM theme_events WHERE kind IN ('create', 'merge') AND created_at >= ?"
        " GROUP BY kind",
        (to_iso(midnight),),
    ).fetchall())
    return ThemeBudget(
        new_themes_left=max(0, MAX_NEW_THEMES_PER_RUN - counts.get("create", 0)),
        merges_left=max(0, MAX_MERGES_PER_RUN - counts.get("merge", 0)),
    )


@dataclass
class ThemeChanges:
    created: int = 0
    assigned: int = 0
    merged: int = 0
    renamed: int = 0
    rejected: list[str] = field(default_factory=list)
    # Messages proposed only for rejected new themes: leave them unthemed so the next run retries.
    unthemed_ids: list[str] = field(default_factory=list)


def apply_proposal(
    conn: sqlite3.Connection, proposal: dict, now: datetime, run_id: int | None, budget: ThemeBudget
) -> ThemeChanges:
    changes = ThemeChanges()
    active = {r["id"] for r in active_themes(conn)}
    placed: set[str] = set()
    orphaned: list[str] = []

    for r in proposal["renames"]:
        if r["theme_id"] not in active or not _clean(r["name"]):
            changes.rejected.append(f"rename {r['theme_id']}: not an active theme or empty name")
            continue
        holder = find_active(conn, r["name"])
        if holder is not None and holder != r["theme_id"]:
            changes.rejected.append(
                f"rename {r['theme_id']} to {_clean(r['name'])!r}: theme {holder} already has that name"
            )
            continue
        rename_theme(conn, r["theme_id"], r["name"], r["description"], now, run_id)
        changes.renamed += 1

    for m in proposal["merges"]:
        src, dst = m["from_id"], m["into_id"]
        if src == dst or src not in active or dst not in active:
            changes.rejected.append(f"merge {src}->{dst}: both themes must be active and different")
            continue
        if budget.merges_left <= 0:
            changes.rejected.append(f"merge {src}->{dst}: daily limit of {MAX_MERGES_PER_RUN} merges reached")
            continue
        merge_themes(conn, src, dst, now, run_id, m["reason"])
        active.discard(src)
        budget.merges_left -= 1
        changes.merged += 1

    for t in proposal["new_themes"]:
        if not _clean(t["name"]):
            changes.rejected.append("new theme with an empty name")
            orphaned += t["message_ids"]
            continue
        tid = find_active(conn, t["name"])
        if tid is None:
            if budget.new_themes_left <= 0:
                changes.rejected.append(
                    f"new theme {t['name']!r}: daily limit of {MAX_NEW_THEMES_PER_RUN} new themes reached"
                )
                orphaned += t["message_ids"]
                continue
            tid, _ = create_theme(conn, t["name"], t["description"], now, run_id)
            budget.new_themes_left -= 1
            changes.created += 1
            active.add(tid)
        for mid in t["message_ids"]:
            changes.assigned += assign(conn, mid, tid)
            placed.add(mid)

    resolved = theme_resolution(conn)
    for a in proposal["assignments"]:
        for tid in a["theme_ids"]:
            root = resolved.get(tid)
            if root is None or root not in active:
                changes.rejected.append(f"assign {a['message_id']}->{tid}: not an active theme")
                continue
            changes.assigned += assign(conn, a["message_id"], root)
            placed.add(a["message_id"])
    changes.unthemed_ids = list(dict.fromkeys(m for m in orphaned if m not in placed))
    return changes
