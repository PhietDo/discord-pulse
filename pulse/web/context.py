"""Template context shared by every page: filters, nav counts, channels, budget and coverage banners."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pulse import stats
from pulse.models import to_iso
from pulse.web.filters import Filters

PATHS = {
    "overview": "/", "pain": "/pain", "bugs": "/bugs", "queue": "/queue", "community": "/community",
    "messages": "/messages", "launch": "/launch", "reports": "/reports", "runs": "/runs",
}


def spend_today(conn: sqlite3.Connection, now: datetime) -> float:
    midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM agent_runs WHERE started_at >= ?", (to_iso(midnight),)
    ).fetchone()
    return float(row["s"])


def open_queue_count(conn: sqlite3.Connection, channels: tuple[str, ...] | None) -> int:
    scope, params = stats.scope_clause(channels)
    return conn.execute(
        "SELECT COUNT(*) FROM mod_queue q JOIN messages m ON m.id = q.message_id"
        f" WHERE q.status = 'open'{scope}",
        params,
    ).fetchone()[0]


def bug_count(conn: sqlite3.Connection, start: datetime, end: datetime, channels: tuple[str, ...] | None) -> int:
    scope, params = stats.scope_clause(channels)
    return conn.execute(
        "SELECT COUNT(*) FROM messages m JOIN triage t ON t.message_id = m.id"
        " WHERE m.is_team = 0 AND m.is_bot = 0 AND t.kind = 'bug'"
        f" AND m.created_at >= ? AND m.created_at < ?{scope}",
        (to_iso(start), to_iso(end), *params),
    ).fetchone()[0]


def base_context(request, conn: sqlite3.Connection, f: Filters, active: str) -> dict:
    settings = request.app.state.settings
    spent = spend_today(conn, f.now)
    cap = settings.config.daily_usd_cap
    open_items = open_queue_count(conn, f.channels)
    nav = [
        ("overview", "Overview", None, False),
        ("pain", "Pain points", len(stats.theme_scores(conn, f.now, limit=1000, channels=f.channels)), False),
        ("bugs", "Bugs", bug_count(conn, f.start, f.end, f.channels), False),
        ("queue", "Mod queue", open_items, open_items > 0),
        ("community", "Community", None, False),
        ("messages", "Messages", None, False),
        ("launch", "Launch", None, False),
        ("reports", "Reports", None, False),
        ("runs", "Runs", None, False),
    ]
    return {
        "f": f,
        "now": f.now,
        "active": active,
        "nav": nav,
        "paths": PATHS,
        "channels": stats.top_channels(conn),
        "server_name": settings.server_name,
        "demo": settings.demo,
        "agents_on": settings.agents_on,
        "agents_off_text": settings.agents_off_text,
        "spent": spent,
        "cap": cap,
        "over_cap": cap > 0 and spent >= cap,
        "coverage": stats.coverage(conn, f.start, f.end, f.channels),
    }
