"""Read-only queries used only by the dashboard."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from pulse import stats
from pulse.models import KINDS, to_iso
from pulse.modqueue import list_open
from pulse.theme_status import LABELS, statuses
from pulse.web.cards import cards_by_ids
from pulse.web.charts import sparkline
from pulse.web.filters import Filters

PER_PAGE = 50
MOODS = {"neg": "t.sentiment < 0", "neutral": "t.sentiment = 0", "pos": "t.sentiment > 0"}


def search_cards(
    conn,
    f: Filters,
    *,
    text: str | None = None,
    author_id: str | None = None,
    theme_id: int | None = None,
    kind: str | None = None,
    mood: str | None = None,
    page: int = 1,
) -> tuple[list[dict], int]:
    where = ["m.created_at >= ?", "m.created_at < ?"]
    params: list = [to_iso(f.start), to_iso(f.end)]
    if text:
        where.append("m.content LIKE ? ESCAPE '\\'")
        params.append(stats._like(text))
    if author_id:
        where.append("m.author_id = ?")
        params.append(author_id)
    if kind in KINDS:
        where.append("t.kind = ?")
        params.append(kind)
    if mood in MOODS:
        where.append(MOODS[mood])
    if theme_id is not None:
        ids = stats.theme_member_ids(conn, theme_id)
        if not ids:
            return [], 0
        where.append(f"m.id IN (SELECT message_id FROM message_themes WHERE theme_id IN ({','.join('?' * len(ids))}))")
        params += ids
    scope, scope_params = stats.scope_clause(f.channels)
    clause = " AND ".join(where) + scope
    params += scope_params
    base = f"FROM messages m LEFT JOIN triage t ON t.message_id = m.id WHERE {clause}"
    total = conn.execute(f"SELECT COUNT(*) {base}", params).fetchone()[0]
    ids = [
        r[0] for r in conn.execute(
            f"SELECT m.id {base} ORDER BY m.created_at DESC, m.id LIMIT ? OFFSET ?",
            (*params, PER_PAGE, (max(page, 1) - 1) * PER_PAGE),
        )
    ]
    return cards_by_ids(conn, ids), total


KIND_COLORS = {
    "bug": "var(--neg)", "docs": "var(--warn)", "feature_request": "var(--team)",
    "other": "var(--faint)", "praise": "var(--pos)", "question": "var(--accent)",
}


def day_keys(start: datetime, end: datetime) -> list[str]:
    day = start.astimezone(timezone.utc).date()
    last = (end - timedelta(microseconds=1)).astimezone(timezone.utc).date()
    out = []
    while day <= last:
        out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def theme_daily(conn, theme_id: int, start: datetime, end: datetime, channels) -> list[int]:
    keys = day_keys(start, end)
    ids = stats.theme_member_ids(conn, theme_id)
    if not ids:
        return [0] * len(keys)
    scope, params = stats.scope_clause(channels)
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT substr(m.created_at, 1, 10) AS day, COUNT(DISTINCT m.id) AS n FROM message_themes mt"
        " JOIN messages m ON m.id = mt.message_id"
        f" WHERE mt.theme_id IN ({marks}) AND m.is_team = 0 AND m.is_bot = 0"
        f" AND m.created_at >= ? AND m.created_at < ?{scope} GROUP BY day",
        (*ids, to_iso(start), to_iso(end), *params),
    ).fetchall()
    by_day = {r["day"]: r["n"] for r in rows}
    return [by_day.get(k, 0) for k in keys]


def _trend_text(volume_prev: int, trend: float) -> str:
    if volume_prev == 0:
        return "new this week"
    if trend >= 2:
        return f"up from {volume_prev} the week before"
    return f"{trend * 100:+.0f}% vs prev 7d"


def theme_rows(conn, f: Filters, limit: int = 20) -> list[dict]:
    current = statuses(conn)
    out = []
    for s in stats.theme_scores(conn, f.now, limit=limit, channels=f.channels):
        st = current.get(s.theme_id, {"status": "new", "label": LABELS["new"], "note": ""})
        out.append({
            "id": s.theme_id, "name": s.name, "description": s.description,
            "volume": s.volume, "prev_volume": s.prev_volume, "negativity": s.mean_negativity,
            "trend": s.trend, "trend_text": _trend_text(s.prev_volume, s.trend), "score": s.score,
            "kinds": s.kinds, "status": st["status"], "status_label": st["label"], "note": st["note"],
            "spark": sparkline(theme_daily(conn, s.theme_id, f.start, f.end, f.channels)),
        })
    return out


def queue_cards(conn, f: Filters, *, limit: int = 200, reason: str | None = None) -> list[dict]:
    items = [r for r in list_open(conn, limit=1000, channels=f.channels) if reason in (None, r["reason"])][:limit]
    cards = {c["message_id"]: c for c in cards_by_ids(conn, [r["message_id"] for r in items])}
    return [
        {**cards[r["message_id"]], "reason": r["reason"], "queue_id": r["queue_id"]}
        for r in items if r["message_id"] in cards
    ]


def open_queue_summary(conn, channels: tuple[str, ...] | None) -> dict:
    """Counts over every open item in scope (not a truncated list): by reason, and the oldest message time."""
    scope, params = stats.scope_clause(channels)
    row = conn.execute(
        "SELECT COUNT(*) AS total, COALESCE(SUM(q.reason = 'frustrated'), 0) AS frustrated,"
        " COALESCE(SUM(q.reason = 'unanswered'), 0) AS unanswered, MIN(m.created_at) AS oldest"
        f" FROM mod_queue q JOIN messages m ON m.id = q.message_id WHERE q.status = 'open'{scope}",
        params,
    ).fetchone()
    return dict(row)


def bug_groups(conn, f: Filters, *, open_only: bool = False, per_group: int = 5) -> list[dict]:
    """Bug reports in the window grouped by pain point (spec 8.4), most urgent pain point first."""
    scope, params = stats.scope_clause(f.channels)
    rows = conn.execute(
        "SELECT m.id, m.author_id, m.thread_id, m.created_at FROM messages m JOIN triage t ON t.message_id = m.id"
        " WHERE m.is_team = 0 AND m.is_bot = 0 AND t.kind = 'bug'"
        f" AND m.created_at >= ? AND m.created_at < ?{scope} ORDER BY m.created_at DESC, m.id",
        (to_iso(f.start), to_iso(f.end), *params),
    ).fetchall()
    resolved = stats.theme_resolution(conn)
    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM themes")}
    theme_of: dict[str, int] = {}
    for r in conn.execute("SELECT message_id, theme_id FROM message_themes ORDER BY theme_id"):
        theme_of.setdefault(r["message_id"], resolved.get(r["theme_id"], r["theme_id"]))
    rank = {s.theme_id: i for i, s in enumerate(stats.theme_scores(conn, f.now, limit=1000, channels=f.channels))}
    groups: dict = {}
    for r in rows:
        tid = theme_of.get(r["id"])
        g = groups.setdefault(tid, {
            "theme_id": tid, "name": names.get(tid, "Not yet grouped") if tid is not None else "Not yet grouped",
            "ids": [], "times": [], "authors": set(), "unanswered_ids": [],
        })
        g["ids"].append(r["id"])
        g["times"].append(r["created_at"])
        g["authors"].add(r["author_id"])
        if stats.first_team_reply(conn, r["id"], r["thread_id"], r["created_at"]) is None:
            g["unanswered_ids"].append(r["id"])
    ordered = sorted(
        groups.values(),
        key=lambda g: (g["theme_id"] is None, rank.get(g["theme_id"], 10**6), -len(g["ids"]), g["name"]),
    )
    out = []
    for g in ordered:
        shown = g["unanswered_ids"] if open_only else g["ids"]
        if not shown:
            continue
        out.append({
            "theme_id": g["theme_id"], "name": g["name"], "count": len(g["ids"]), "people": len(g["authors"]),
            "unanswered": len(g["unanswered_ids"]), "unanswered_ids": set(g["unanswered_ids"]),
            "latest": g["times"][0], "first": g["times"][-1],
            "cards": cards_by_ids(conn, shown[:per_group]), "more": max(0, len(shown) - per_group),
        })
    return out


def launch_markers(conn, start: datetime, end: datetime) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT name, date FROM launches WHERE date >= ? AND date <= ? ORDER BY date",
        (start.date().isoformat(), end.date().isoformat()),
    ).fetchall()
    return [(r["date"], r["name"]) for r in rows]


def kind_mix(by_kind: dict) -> list[dict]:
    total = sum(by_kind.values())
    ordered = sorted((kv for kv in by_kind.items() if kv[1]), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"kind": k, "count": n, "pct": round(100 * n / total), "color": KIND_COLORS.get(k, "var(--faint)")}
        for k, n in ordered
    ]


def latest_digest(conn) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM digests ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()


def pct_change(current: int, previous: int) -> int | None:
    return None if previous == 0 else round(100 * (current - previous) / previous)


JOB_TIMEOUT_MINUTES = 10


def report_rows(conn) -> list[dict]:
    rows = []
    for r in conn.execute(
        "SELECT d.id, d.kind, d.created_at, l.name AS launch FROM digests d LEFT JOIN launches l ON l.id = d.launch_id"
    ):
        title = f"Launch digest: {r['launch']}" if r["kind"] == "launch" else "Weekly digest"
        rows.append({"type": "digest", "id": r["id"], "title": title, "created_at": r["created_at"],
                     "state": "done", "href": f"/reports/digest/{r['id']}"})
    for r in conn.execute("SELECT id, question, markdown, created_at FROM investigations"):
        md = r["markdown"]
        state = "running" if md is None else "failed" if md.startswith("Investigation failed") else "done"
        rows.append({"type": "investigation", "id": r["id"], "title": r["question"], "created_at": r["created_at"],
                     "state": state, "href": f"/reports/investigation/{r['id']}"})
    rows.sort(key=lambda x: (x["created_at"], x["type"], x["id"]), reverse=True)
    return rows


def digest_job_state(conn, since: datetime, now: datetime) -> tuple[str, str | None]:
    since_iso = to_iso(since)
    if conn.execute("SELECT 1 FROM digests WHERE created_at >= ? LIMIT 1", (since_iso,)).fetchone():
        return "done", None
    failed = conn.execute(
        "SELECT error, status FROM agent_runs WHERE agent = 'digest' AND status != 'ok' AND started_at >= ?"
        " ORDER BY id DESC LIMIT 1",
        (since_iso,),
    ).fetchone()
    if failed:
        reason = "the daily budget cap was reached" if failed["status"] == "skipped_budget" else (failed["error"] or "the digest agent failed")
        return "failed", reason
    if now - since > timedelta(minutes=JOB_TIMEOUT_MINUTES):
        return "failed", f"no digest after {JOB_TIMEOUT_MINUTES} minutes"
    return "running", None


RUN_STATUSES = ("ok", "failed", "skipped_budget")


def daily_costs(conn, now: datetime, days: int = 14) -> list[dict]:
    end = (now.astimezone(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    rows = conn.execute(
        "SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS runs, COALESCE(SUM(cost_usd), 0) AS cost,"
        " SUM(status != 'ok') AS failed FROM agent_runs WHERE started_at >= ? AND started_at < ? GROUP BY day",
        (to_iso(start), to_iso(end)),
    ).fetchall()
    by_day = {r["day"]: r for r in rows}
    out = []
    for key in day_keys(start, end):
        r = by_day.get(key)
        out.append({"day": key, "runs": r["runs"] if r else 0, "cost": float(r["cost"]) if r else 0.0,
                    "failed": r["failed"] if r else 0})
    return out


def recent_runs(conn, *, status: str | None = None, limit: int = 100):
    if status in RUN_STATUSES:
        return conn.execute(
            "SELECT * FROM agent_runs WHERE status = ? ORDER BY started_at DESC, id DESC LIMIT ?", (status, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM agent_runs ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
