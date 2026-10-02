"""Slack alerts: pain points spiking, and frustrated users left without a staff reply.

Each alert is sent once (keyed in alerts_sent), and only recorded after Slack accepts it.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

import httpx

from pulse.config import Config
from pulse.links import jump_link
from pulse.models import from_iso, to_iso
from pulse.stats import first_team_reply, sample_messages, theme_scores

SLACK_ENV = "SLACK_WEBHOOK_URL"
MAX_PER_RUN = 10
TIMEOUT = 20.0


@dataclass(frozen=True)
class Alert:
    kind: str
    key: str
    text: str


@dataclass
class AlertStats:
    found: int = 0
    sent: int = 0
    failed: int = 0
    skipped: str | None = None


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _excerpt(text: str, limit: int = 140) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _link(conn: sqlite3.Connection, message_id: str) -> str:
    r = conn.execute("SELECT guild_id, channel_id FROM messages WHERE id = ?", (message_id,)).fetchone()
    return jump_link(r["guild_id"], r["channel_id"], message_id)


def find_alerts(conn: sqlite3.Connection, config: Config, now: datetime) -> list[Alert]:
    cfg = config.alerts
    sent = {(r["kind"], r["key"]) for r in conn.execute("SELECT kind, key FROM alerts_sent")}
    out: list[Alert] = []
    day = now.astimezone(timezone.utc).date().isoformat()
    for s in theme_scores(conn, now, window_days=1, limit=50):
        key = f"{s.theme_id}:{day}"
        if s.volume < cfg.spike_min_volume or s.trend < cfg.spike_trend or ("spike", key) in sent:
            continue
        text = (f":chart_with_upwards_trend: Pain point spiking: *{_esc(s.name)}*: {s.volume} messages in the "
                f"last 24 hours (previous 24 hours: {s.prev_volume}).")
        top = sample_messages(conn, now - timedelta(days=1), now, theme_id=s.theme_id, limit=1)
        if top:
            text += f"\n> {_esc(_excerpt(top[0]['content']))} <{_link(conn, top[0]['message_id'])}|Open in Discord>"
        out.append(Alert("spike", key, text))
    cutoff = to_iso(now - timedelta(hours=cfg.frustrated_hours))
    rows = conn.execute(
        "SELECT q.id, m.id AS message_id, m.thread_id, m.channel_name, m.author_name, m.content, m.created_at"
        " FROM mod_queue q JOIN messages m ON m.id = q.message_id"
        " WHERE q.status = 'open' AND q.reason = 'frustrated' AND m.created_at <= ?"
        " ORDER BY m.created_at, q.id",
        (cutoff,),
    ).fetchall()
    for r in rows:
        key = str(r["id"])
        if ("frustrated", key) in sent:
            continue
        if first_team_reply(conn, r["message_id"], r["thread_id"], r["created_at"], before=to_iso(now)):
            continue
        hours = int((now - from_iso(r["created_at"])).total_seconds() // 3600)
        text = (f":rotating_light: Frustrated and unanswered for {hours} hours: {_esc(r['author_name'])} in "
                f"#{_esc(r['channel_name'])}\n> {_esc(_excerpt(r['content']))} "
                f"<{_link(conn, r['message_id'])}|Open in Discord>")
        out.append(Alert("frustrated", key, text))
    return out


def send_alerts(conn: sqlite3.Connection, config: Config, now: datetime, *,
                client: httpx.Client | None = None, env: Mapping[str, str] | None = None) -> AlertStats:
    stats = AlertStats()
    alerts = find_alerts(conn, config, now)
    stats.found = len(alerts)
    if not alerts:
        return stats
    env = os.environ if env is None else env
    url = env.get(SLACK_ENV)
    if not url:
        stats.skipped = f"{SLACK_ENV} is not set"
        return stats
    own = client is None
    client = client or httpx.Client()
    try:
        for alert in alerts[:MAX_PER_RUN]:
            try:
                ok = client.post(url, json={"text": alert.text}, timeout=TIMEOUT).status_code < 300
            except httpx.HTTPError:
                ok = False
            if not ok:
                stats.failed += 1
                break  # the rest go next run
            with conn:
                conn.execute("INSERT OR IGNORE INTO alerts_sent (kind, key, sent_at) VALUES (?, ?, ?)",
                             (alert.kind, alert.key, to_iso(now)))
            stats.sent += 1
    finally:
        if own:
            client.close()
    return stats
