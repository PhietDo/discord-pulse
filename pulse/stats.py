"""Read-only aggregates over triaged messages, shared by the digest, Investigate and the dashboard.

Windows are [start, end) in UTC. Only non-team, non-bot, triaged messages count.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pulse.models import KINDS, to_iso

MESSAGE_COLUMNS = (
    "m.id AS message_id, m.author_name AS author, m.channel_name AS channel, m.created_at,"
    " t.kind, t.sentiment, m.content"
)
_COMMUNITY = "m.is_team = 0 AND m.is_bot = 0"


def _like(text: str) -> str:
    """A LIKE pattern matching text as a literal substring. Use with ESCAPE '\\'."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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


def period_summary(conn: sqlite3.Connection, start: datetime, end: datetime) -> dict:
    rows = conn.execute(
        "SELECT t.sentiment, t.kind, t.needs_reply FROM messages m JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?",
        (to_iso(start), to_iso(end)),
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


def sentiment_series(conn: sqlite3.Connection, start: datetime, end: datetime) -> list[dict]:
    rows = conn.execute(
        "SELECT substr(m.created_at, 1, 10) AS day, COUNT(*) AS n, AVG(t.sentiment) AS avg"
        " FROM messages m JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ? GROUP BY day",
        (to_iso(start), to_iso(end)),
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
    rows = conn.execute(
        "SELECT mt.theme_id, m.id AS message_id, m.created_at, t.sentiment, t.kind"
        " FROM message_themes mt JOIN messages m ON m.id = mt.message_id"
        " JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?",
        (to_iso(prev_start), to_iso(now)),
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
    if kinds:
        sql += f" AND t.kind IN ({','.join('?' * len(kinds))})"
        params += list(kinds)
    order = "t.sentiment ASC" if most_negative else "t.sentiment DESC"
    sql += f" ORDER BY {order}, m.created_at DESC, m.id LIMIT ?"
    params.append(limit)
    return [to_message(r) for r in conn.execute(sql, params)]
