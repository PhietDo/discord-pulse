"""A single self-contained HTML report (spec 16.3): inline CSS, server-drawn SVG, no JavaScript."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup

from pulse import community, stats
from pulse.citations import render_html
from pulse.config import Config
from pulse.theme_status import statuses
from pulse.web import charts, fmt, queries
from pulse.web.cards import cards_by_ids
from pulse.web.context import open_queue_count

_WEB = Path(__file__).parent / "web"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(_WEB / "templates"), autoescape=select_autoescape())
    env.filters.update(age=fmt.age, utc=fmt.utc, signed=fmt.signed, minutes=fmt.minutes, kind_label=fmt.kind_label)
    return env


def _author(card: dict, with_names: bool) -> str:
    if with_names:
        return card["author"]
    return "staff" if card["is_team"] else "a member"


def build_report(conn: sqlite3.Connection, config: Config, now: datetime, *, days: int = 7,
                 with_names: bool = False, server_name: str = "Discord server") -> str:
    start = now - timedelta(days=days)
    current, before = stats.period_summary(conn, start, now), stats.period_summary(conn, start - timedelta(days=days), start)
    status_by_theme = statuses(conn)
    pains = [
        {"name": s.name, "volume": s.volume, "prev": s.prev_volume, "score": s.score,
         "status": status_by_theme[s.theme_id]["label"] if s.theme_id in status_by_theme else "Not triaged",
         "spark": charts.sparkline(queries.theme_daily(conn, s.theme_id, start, now, None))}
        for s in stats.theme_scores(conn, now, limit=8)
    ]
    heat = community.activity_heatmap(conn, start, now, config.timezone)
    nc = community.newcomers(conn, start, now)
    board = community.helpers(conn, start, now)
    if not with_names:
        board = [{**h, "author_name": f"Helper {i}"} for i, h in enumerate(board, 1)]
    wanted = community.most_wanted(conn, start, now)
    cards = {c["message_id"]: c for c in cards_by_ids(conn, [w["message_id"] for w in wanted])}
    wanted_rows = [
        {"content": cards[w["message_id"]]["content"][:300], "link": cards[w["message_id"]]["link"],
         "author": _author(cards[w["message_id"]], with_names), "emojis": w["emojis"], "total": w["total"]}
        for w in wanted if w["message_id"] in cards
    ]
    digest = queries.latest_digest(conn)
    digest_html = Markup(render_html(digest["markdown"], conn, anonymize=not with_names)) if digest else None
    return _env().get_template("report.html").render(
        css=Markup((_WEB / "static" / "pulse.css").read_text(encoding="utf-8")),
        server_name=server_name, days=days, start=start, now=now, with_names=with_names,
        generated=now.strftime("%Y-%m-%d %H:%M UTC"),
        cur=current, before=before, open_queue=open_queue_count(conn, None),
        chart=charts.sentiment_chart(stats.sentiment_series(conn, start, now), queries.launch_markers(conn, start, now)),
        pains=pains, tz=config.timezone, gaps=community.coverage_gaps(heat),
        heat_questions=charts.heatmap(heat["questions"], "Questions needing a reply by weekday and hour"),
        nc=nc, helpers=board, wanted=wanted_rows, digest_html=digest_html,
    )
