"""Community analytics (spec 16): when questions arrive vs when staff answer, newcomers,
community helpers, and what people want most. Plain SQL and Python; no model calls."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pulse.models import from_iso, to_iso
from pulse.stats import scope_clause

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
REPLY_WINDOW = timedelta(hours=48)
RETURN_AFTER = timedelta(hours=24)
_CHUNK = 500


def _empty_grid() -> list[list[int]]:
    return [[0] * 24 for _ in range(7)]


def activity_heatmap(conn: sqlite3.Connection, start: datetime, end: datetime, tz: str = "UTC",
                     channels: tuple[str, ...] | None = None) -> dict:
    """Weekday x hour counts in `tz`: community messages labelled needs-reply ("questions")
    and staff messages ("staff"). Bots are ignored."""
    zone = ZoneInfo(tz)
    scope, params = scope_clause(channels)
    questions, staff = _empty_grid(), _empty_grid()
    rows = conn.execute(
        "SELECT m.created_at, m.is_team, COALESCE(t.needs_reply, 0) AS needs_reply FROM messages m"
        " LEFT JOIN triage t ON t.message_id = m.id"
        f" WHERE m.is_bot = 0 AND m.created_at >= ? AND m.created_at < ?{scope}",
        (to_iso(start), to_iso(end), *params),
    )
    for r in rows:
        local = from_iso(r["created_at"]).astimezone(zone)
        if r["is_team"]:
            staff[local.weekday()][local.hour] += 1
        elif r["needs_reply"]:
            questions[local.weekday()][local.hour] += 1
    return {"questions": questions, "staff": staff, "timezone": tz}


def coverage_gaps(heat: dict, top: int = 3) -> list[dict]:
    """Hours with questions and the fewest staff messages; ties go to more questions."""
    cells = sorted(
        (heat["staff"][d][h], -heat["questions"][d][h], d, h)
        for d in range(7) for h in range(24) if heat["questions"][d][h] > 0
    )
    return [
        {"label": f"{WEEKDAYS[d]} {h:02d}:00–{(h + 1) % 24:02d}:00", "questions": -neg_q, "staff": s}
        for s, neg_q, d, h in cells[:top]
    ]


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def newcomers(conn: sqlite3.Connection, start: datetime, end: datetime,
              channels: tuple[str, ...] | None = None) -> dict:
    """Authors whose first community message (in the imported history, within the channel
    scope) falls in [start, end)."""
    scope, params = scope_clause(channels)
    rows = conn.execute(
        "SELECT m.id, m.author_id, m.thread_id, m.created_at FROM messages m"
        " JOIN (SELECT m.author_id, MIN(m.created_at) AS first FROM messages m"
        f"       WHERE m.is_team = 0 AND m.is_bot = 0{scope} GROUP BY m.author_id) f"
        "   ON f.author_id = m.author_id AND f.first = m.created_at"
        f" WHERE m.is_team = 0 AND m.is_bot = 0{scope} AND m.created_at >= ? AND m.created_at < ?"
        " ORDER BY m.created_at, m.id",
        (*params, *params, to_iso(start), to_iso(end)),
    ).fetchall()
    firsts, seen = [], set()
    for r in rows:
        if r["author_id"] not in seen:
            seen.add(r["author_id"])
            firsts.append(r)
    weeks: dict[str, int] = {}
    week, last = _monday(start.date()), _monday(max(start, end - timedelta(microseconds=1)).date())
    while week <= last:
        weeks[week.isoformat()] = 0
        week += timedelta(days=7)
    replied = returned = 0
    unreplied: list[str] = []
    for r in firsts:
        at = from_iso(r["created_at"])
        key = _monday(at.date()).isoformat()
        weeks[key] = weeks.get(key, 0) + 1
        got_reply = conn.execute(
            "SELECT 1 FROM messages x WHERE x.author_id != ? AND x.is_bot = 0"
            " AND x.created_at > ? AND x.created_at <= ?"
            " AND (x.reply_to_id = ? OR x.thread_id = ? OR (? IS NOT NULL AND x.thread_id = ?)) LIMIT 1",
            (r["author_id"], r["created_at"], to_iso(at + REPLY_WINDOW), r["id"], r["id"],
             r["thread_id"], r["thread_id"]),
        ).fetchone()
        if got_reply:
            replied += 1
        else:
            unreplied.append(r["id"])
        came_back = conn.execute(
            "SELECT 1 FROM messages WHERE author_id = ? AND created_at >= ? LIMIT 1",
            (r["author_id"], to_iso(at + RETURN_AFTER)),
        ).fetchone()
        returned += came_back is not None
    return {
        "count": len(firsts),
        "replied": replied,
        "returned": returned,
        "weekly": [{"week": k, "count": v} for k, v in sorted(weeks.items())],
        "unreplied_ids": list(reversed(unreplied))[:10],
    }


def _rows_by_id(conn: sqlite3.Connection, ids, select: str) -> dict[str, sqlite3.Row]:
    ids = list(ids)
    out: dict[str, sqlite3.Row] = {}
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        for r in conn.execute(f"{select} WHERE m.id IN ({','.join('?' * len(chunk))})", chunk):
            out[r["id"]] = r
    return out


def helpers(conn: sqlite3.Connection, start: datetime, end: datetime,
            channels: tuple[str, ...] | None = None, limit: int = 10) -> list[dict]:
    """Non-staff members ranked by answers to other people's needs-reply messages."""
    scope, params = scope_clause(channels)
    answers = conn.execute(
        "SELECT m.id, m.author_id, m.author_name, m.created_at, m.reply_to_id, m.thread_id FROM messages m"
        f" WHERE m.is_team = 0 AND m.is_bot = 0 AND m.created_at >= ? AND m.created_at < ?{scope}"
        " ORDER BY m.created_at, m.id",
        (to_iso(start), to_iso(end), *params),
    ).fetchall()
    thread_first = {
        r["thread_id"]: r["id"]
        for r in conn.execute(
            "SELECT thread_id, id, MIN(created_at) FROM messages WHERE thread_id IS NOT NULL GROUP BY thread_id"
        )
    }

    def candidates(a) -> list[str]:
        if a["reply_to_id"]:
            return [a["reply_to_id"]]
        if a["thread_id"]:
            return [c for c in (a["thread_id"], thread_first.get(a["thread_id"])) if c and c != a["id"]]
        return []

    questions = _rows_by_id(
        conn, {c for a in answers for c in candidates(a)},
        "SELECT m.id, m.author_id, m.created_at, COALESCE(t.needs_reply, 0) AS needs_reply"
        " FROM messages m LEFT JOIN triage t ON t.message_id = m.id",
    )
    board: dict[str, dict] = {}
    for a in answers:
        for qid in candidates(a):
            q = questions.get(qid)
            if q is None or not q["needs_reply"] or q["author_id"] == a["author_id"] or q["created_at"] >= a["created_at"]:
                continue
            entry = board.setdefault(a["author_id"], {"author_id": a["author_id"], "answers": 0, "helped": set()})
            entry.update(author_name=a["author_name"], last_at=a["created_at"], sample_id=a["id"])
            entry["answers"] += 1
            entry["helped"].add(q["author_id"])
            break
    ranked = sorted(board.values(), key=lambda e: (-e["answers"], -len(e["helped"]), e["author_name"].lower()))
    return [{**e, "helped": len(e["helped"])} for e in ranked[:limit]]


def _reacted(conn, start, end, channels, kinds, limit) -> list[dict]:
    scope, params = scope_clause(channels)
    kind_sql = f" AND t.kind IN ({','.join('?' * len(kinds))})" if kinds else ""
    rows = conn.execute(
        "SELECT m.id, SUM(r.count) AS total FROM messages m JOIN reactions r ON r.message_id = m.id"
        " LEFT JOIN triage t ON t.message_id = m.id"
        f" WHERE m.is_bot = 0 AND m.created_at >= ? AND m.created_at < ?{scope}{kind_sql}"
        " GROUP BY m.id ORDER BY total DESC, m.created_at DESC, m.id LIMIT ?",
        (to_iso(start), to_iso(end), *params, *(kinds or ()), limit),
    ).fetchall()
    out = []
    for r in rows:
        emojis = [
            (e["emoji"], e["count"])
            for e in conn.execute(
                "SELECT emoji, count FROM reactions WHERE message_id = ? ORDER BY count DESC, emoji LIMIT 4",
                (r["id"],),
            )
        ]
        out.append({"message_id": r["id"], "total": r["total"], "emojis": emojis})
    return out


def most_wanted(conn, start, end, channels=None, limit: int = 8) -> list[dict]:
    """Feature requests ranked by total reactions."""
    return _reacted(conn, start, end, channels, ("feature_request",), limit)


def top_reacted(conn, start, end, channels=None, limit: int = 5) -> list[dict]:
    """The most reacted messages of any kind."""
    return _reacted(conn, start, end, channels, None, limit)
