"""Read-only aggregates over triaged messages, shared by the digest, Investigate and the dashboard.

Windows are [start, end) in UTC. Only non-team, non-bot, triaged messages count.
"""
from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pulse.models import KINDS, from_iso, to_iso

MESSAGE_COLUMNS = (
    "m.id AS message_id, m.author_name AS author, m.channel_name AS channel, m.created_at,"
    " t.kind, t.sentiment, m.content"
)
_COMMUNITY = "m.is_team = 0 AND m.is_bot = 0"


def _like(text: str) -> str:
    """A LIKE pattern matching text as a literal substring. Use with ESCAPE '\\'."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def scope_clause(channels: tuple[str, ...] | None) -> tuple[str, list]:
    """SQL limiting messages `m` to the given top-level channels and their threads.

    Returns ("", []) when unscoped. Rows imported before parent_channel_id existed count
    under their own channel id until they are re-imported.
    """
    if not channels:
        return "", []
    marks = ",".join("?" * len(channels))
    return f" AND (m.channel_id IN ({marks}) OR m.parent_channel_id IN ({marks}))", [*channels, *channels]


def theme_clause(conn: sqlite3.Connection, theme_id: int | None) -> tuple[str, list] | None:
    """SQL limiting messages `m` to a pain point, merged themes included.

    ("", []) when theme_id is None; None when the theme is unknown.
    """
    if theme_id is None:
        return "", []
    ids = theme_member_ids(conn, theme_id)
    if not ids:
        return None
    marks = ",".join("?" * len(ids))
    return f" AND m.id IN (SELECT message_id FROM message_themes WHERE theme_id IN ({marks}))", ids


def coverage(conn: sqlite3.Connection, start: datetime, end: datetime,
             channels: tuple[str, ...] | None = None) -> dict:
    """Community messages in [start, end) and how many of them are triaged. Triage runs
    newest first under the daily cap, so older ranges can be partly analyzed."""
    scope, params = scope_clause(channels)
    row = conn.execute(
        "SELECT COUNT(*) AS total, COUNT(t.message_id) AS triaged FROM messages m"
        " LEFT JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND trim(m.content) != '' AND m.created_at >= ? AND m.created_at < ?{scope}",
        (to_iso(start), to_iso(end), *params),
    ).fetchone()
    return {"total": row["total"], "triaged": row["triaged"]}


def top_channels(conn: sqlite3.Connection) -> list[dict]:
    """Top-level channels with community messages, threads counted under their parent,
    busiest first. A forum channel that only has threads is named from its threads'
    parent_channel_name (its id if that is unknown). Channels where only staff or bots post
    are left out. Thread rows imported before parent_channel_id existed are left out until
    they are re-imported."""
    rows = conn.execute(
        "SELECT COALESCE(parent_channel_id, channel_id) AS cid,"
        " COALESCE(MAX(CASE WHEN thread_id IS NULL THEN channel_name END), MAX(parent_channel_name)) AS name,"
        " COUNT(*) AS n FROM messages"
        " WHERE thread_id IS NULL OR parent_channel_id IS NOT NULL GROUP BY cid"
        " HAVING SUM(is_team = 0 AND is_bot = 0) > 0 ORDER BY n DESC, cid"
    ).fetchall()
    return [{"id": r["cid"], "name": r["name"] or r["cid"], "messages": r["n"]} for r in rows]


def to_message(row: sqlite3.Row) -> dict:
    content = row["content"]
    return {
        "message_id": row["message_id"],
        "author": row["author"],
        "channel": row["channel"],
        "created_at": row["created_at"],
        "kind": row["kind"],
        "sentiment": row["sentiment"],
        "content": content if len(content) <= 500 else content[:500] + "…",
    }


def theme_resolution(conn: sqlite3.Connection) -> dict[int, int]:
    parent = {r["id"]: r["merged_into"] for r in conn.execute("SELECT id, merged_into FROM themes")}
    resolved: dict[int, int] = {}
    for tid in parent:
        seen: set[int] = set()
        cur = tid
        while parent.get(cur) is not None and cur not in seen:
            seen.add(cur)
            cur = parent[cur]
        resolved[tid] = cur
    return resolved


def theme_member_ids(conn: sqlite3.Connection, theme_id: int) -> list[int]:
    resolved = theme_resolution(conn)
    if theme_id not in resolved:
        return []
    root = resolved[theme_id]
    return sorted(tid for tid, r in resolved.items() if r == root)


def period_summary(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    *,
    channels: tuple[str, ...] | None = None,
    theme_id: int | None = None,
) -> dict:
    scope, scope_params = scope_clause(channels)
    theme = theme_clause(conn, theme_id)
    theme_sql, theme_params = theme if theme is not None else (" AND 0", [])
    rows = conn.execute(
        "SELECT t.sentiment, t.kind, t.needs_reply FROM messages m JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?{scope}{theme_sql}",
        (to_iso(start), to_iso(end), *scope_params, *theme_params),
    ).fetchall()
    n = len(rows)
    by_kind = {k: 0 for k in KINDS}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    return {
        "start": to_iso(start),
        "end": to_iso(end),
        "messages": n,
        "avg_sentiment": round(sum(r["sentiment"] for r in rows) / n, 3) if n else None,
        "negative": sum(1 for r in rows if r["sentiment"] < 0),
        "needs_reply": sum(1 for r in rows if r["needs_reply"]),
        "by_kind": by_kind,
    }


def sentiment_series(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    *,
    channels: tuple[str, ...] | None = None,
    theme_id: int | None = None,
) -> list[dict]:
    scope, scope_params = scope_clause(channels)
    theme = theme_clause(conn, theme_id)
    theme_sql, theme_params = theme if theme is not None else (" AND 0", [])
    rows = conn.execute(
        "SELECT substr(m.created_at, 1, 10) AS day, COUNT(*) AS n, AVG(t.sentiment) AS avg"
        " FROM messages m JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?{scope}{theme_sql} GROUP BY day",
        (to_iso(start), to_iso(end), *scope_params, *theme_params),
    ).fetchall()
    by_day = {r["day"]: r for r in rows}
    out = []
    day = start.astimezone(timezone.utc).date()
    last = (end - timedelta(microseconds=1)).astimezone(timezone.utc).date()
    while day <= last:
        key = day.isoformat()
        r = by_day.get(key)
        out.append({
            "day": key,
            "messages": r["n"] if r else 0,
            "avg_sentiment": round(r["avg"], 3) if r else None,
        })
        day += timedelta(days=1)
    return out


@dataclass(frozen=True)
class ThemeScore:
    theme_id: int
    name: str
    description: str
    volume: int
    prev_volume: int
    mean_negativity: float
    trend: float
    score: float
    kinds: dict


def theme_scores(
    conn: sqlite3.Connection,
    now: datetime,
    *,
    window_days: int = 7,
    limit: int = 20,
    start: datetime | None = None,
    channels: tuple[str, ...] | None = None,
) -> list[ThemeScore]:
    """Active themes ranked by score = volume x mean negativity x (1 + positive trend).

    The current window is [now - window_days, now), or [start, now) when start is given
    (window_days is then ignored); the previous window is the equal-length span just before
    it. Ties are broken by higher volume, then lower theme id.
    """
    cur_start = start if start is not None else now - timedelta(days=window_days)
    prev_start = cur_start - (now - cur_start)
    resolved = theme_resolution(conn)
    active = {
        r["id"]: r for r in conn.execute("SELECT id, name, description FROM themes WHERE status = 'active'")
    }
    scope, scope_params = scope_clause(channels)
    rows = conn.execute(
        "SELECT mt.theme_id, m.id AS message_id, m.created_at, t.sentiment, t.kind"
        " FROM message_themes mt JOIN messages m ON m.id = mt.message_id"
        " JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?{scope}",
        (to_iso(prev_start), to_iso(now), *scope_params),
    ).fetchall()
    cur_iso = to_iso(cur_start)
    agg: dict[int, dict] = {}
    counted: set[tuple[int, str]] = set()
    for r in rows:
        root = resolved.get(r["theme_id"], r["theme_id"])
        if root not in active or (root, r["message_id"]) in counted:
            continue
        counted.add((root, r["message_id"]))
        a = agg.setdefault(root, {"cur": [], "prev": 0, "kinds": {}})
        if r["created_at"] >= cur_iso:
            a["cur"].append(r["sentiment"])
            a["kinds"][r["kind"]] = a["kinds"].get(r["kind"], 0) + 1
        else:
            a["prev"] += 1

    scores = []
    for tid, a in agg.items():
        volume = len(a["cur"])
        if volume == 0:
            continue
        negativity = sum(max(0, -s) for s in a["cur"]) / volume
        trend = (volume - a["prev"]) / max(a["prev"], 1)
        scores.append(ThemeScore(
            theme_id=tid,
            name=active[tid]["name"],
            description=active[tid]["description"],
            volume=volume,
            prev_volume=a["prev"],
            mean_negativity=round(negativity, 3),
            trend=round(trend, 3),
            score=round(volume * negativity * (1 + max(0.0, trend)), 3),
            kinds=dict(sorted(a["kinds"].items())),
        ))
    scores.sort(key=lambda s: (-s.score, -s.volume, s.theme_id))
    return scores[:limit]


def queue_counts(conn: sqlite3.Connection) -> dict:
    by_reason = {
        r["reason"]: r["n"]
        for r in conn.execute("SELECT reason, COUNT(*) AS n FROM mod_queue WHERE status = 'open' GROUP BY reason")
    }
    return {
        "open": sum(by_reason.values()),
        "frustrated": by_reason.get("frustrated", 0),
        "unanswered": by_reason.get("unanswered", 0),
    }


def sample_messages(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    *,
    theme_id: int | None = None,
    kinds: tuple[str, ...] | None = None,
    limit: int = 10,
    most_negative: bool = True,
    channels: tuple[str, ...] | None = None,
) -> list[dict]:
    sql = f"SELECT DISTINCT {MESSAGE_COLUMNS} FROM messages m JOIN triage t ON t.message_id = m.id"
    params: list = []
    if theme_id is not None:
        ids = theme_member_ids(conn, theme_id)
        if not ids:
            return []
        sql += f" JOIN message_themes mt ON mt.message_id = m.id AND mt.theme_id IN ({','.join('?' * len(ids))})"
        params += ids
    sql += f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?"
    params += [to_iso(start), to_iso(end)]
    scope, scope_params = scope_clause(channels)
    sql += scope
    params += scope_params
    if kinds:
        sql += f" AND t.kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    order = "t.sentiment ASC" if most_negative else "t.sentiment DESC"
    sql += f" ORDER BY {order}, m.created_at DESC, m.id LIMIT ?"
    params.append(limit)
    return [to_message(r) for r in conn.execute(sql, params)]


REPLY_SLA_HOURS = 24


def first_team_reply(
    conn: sqlite3.Connection, message_id: str, thread_id: str | None, created_at: str, *, before: str | None = None
) -> str | None:
    """ISO time of the earliest staff message after this one that replies to it, is in its
    thread, or is in the thread started from it (the mod queue's matching). None if none."""
    sql = (
        "SELECT MIN(r.created_at) AS first FROM messages r WHERE r.is_team = 1 AND r.created_at > ?"
        " AND (r.reply_to_id = ? OR (? IS NOT NULL AND r.thread_id = ?) OR r.thread_id = ?)"
    )
    params = [created_at, message_id, thread_id, thread_id, message_id]
    if before is not None:
        sql += " AND r.created_at <= ?"
        params.append(before)
    row = conn.execute(sql, params).fetchone()
    return row["first"]


def reply_stats(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    channels: tuple[str, ...] | None = None,
) -> dict:
    """How quickly staff answer community messages that need a reply (spec 15.1)."""
    scope, scope_params = scope_clause(channels)
    rows = conn.execute(
        "SELECT m.id, m.thread_id, m.created_at FROM messages m JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND t.needs_reply = 1 AND m.created_at >= ? AND m.created_at < ?{scope}",
        (to_iso(start), to_iso(end), *scope_params),
    ).fetchall()
    cutoff = to_iso(now - timedelta(hours=REPLY_SLA_HOURS))
    waits: list[float] = []
    waiting = 0
    now_iso = to_iso(now)
    for r in rows:
        first = first_team_reply(conn, r["id"], r["thread_id"], r["created_at"], before=now_iso)
        if first is not None:
            waits.append((from_iso(first) - from_iso(r["created_at"])).total_seconds() / 60)
        elif r["created_at"] <= cutoff:
            waiting += 1
    return {
        "needs_reply": len(rows),
        "answered": len(waits),
        "median_minutes": round(statistics.median(waits), 1) if waits else None,
        "waiting_over_24h": waiting,
    }


def channel_breakdown(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
    now: datetime,
    *,
    channels: tuple[str, ...] | None = None,
) -> list[dict]:
    """Per top-level channel (threads included): volume, mood and reply times for the window.
    With `channels`, only those channels are computed."""
    out = []
    for ch in top_channels(conn):
        if channels is not None and ch["id"] not in channels:
            continue
        summary = period_summary(conn, start, end, channels=(ch["id"],))
        if not summary["messages"]:
            continue
        replies = reply_stats(conn, start, end, now, channels=(ch["id"],))
        out.append({
            "id": ch["id"],
            "name": ch["name"],
            "messages": summary["messages"],
            "avg_sentiment": summary["avg_sentiment"],
            "negative_share": round(summary["negative"] / summary["messages"], 3),
            "needs_reply": replies["needs_reply"],
            "median_reply_minutes": replies["median_minutes"],
            "waiting_over_24h": replies["waiting_over_24h"],
        })
    out.sort(key=lambda c: (-c["messages"], c["id"]))
    return out
