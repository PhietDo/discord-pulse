"""Digest agent: weekly and per-launch reports that cite real messages."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from pulse import stats
from pulse.agents.llm import LLMClient
from pulse.citations import cited_ids, strip_unknown
from pulse.models import to_iso
from pulse.modqueue import list_open

SCHEMA_NAME = "digest_result"
MAX_MESSAGES = 60
LAUNCH_WINDOW_DAYS = 14
QUEUE_ITEMS = 8
EMPTY_MARKDOWN = "_No community activity in this period._"
DIGEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["markdown"],
    "properties": {"markdown": {"type": "string"}},
}


@dataclass(frozen=True)
class DigestResult:
    digest_id: int
    kind: str
    period_start: str
    period_end: str
    markdown: str
    cited_message_ids: list[str]
    removed_citations: list[str]


def load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "digest_v1.md").read_text(encoding="utf-8")


def _dedupe(messages: list[dict], cap: int = MAX_MESSAGES) -> list[dict]:
    seen: set[str] = set()
    out = []
    for m in messages:
        if m["message_id"] in seen:
            continue
        seen.add(m["message_id"])
        out.append(m)
        if len(out) >= cap:
            break
    return out


def _queue_messages(conn: sqlite3.Connection) -> list[dict]:
    """The most urgent open mod queue items, tagged so "Needs attention" can cite them."""
    out = []
    for item in list_open(conn, limit=QUEUE_ITEMS):
        row = conn.execute(
            f"SELECT {stats.MESSAGE_COLUMNS} FROM messages m JOIN triage t ON t.message_id = m.id WHERE m.id = ?",
            (item["message_id"],),
        ).fetchone()
        if row is not None:
            out.append({**stats.to_message(row), "queue_reason": item["reason"]})
    return out


def build_weekly_input(conn: sqlite3.Connection, now: datetime) -> dict:
    start = now - timedelta(days=7)
    previous_start = start - timedelta(days=7)
    top = stats.theme_scores(conn, now, window_days=7, limit=10)
    messages: list[dict] = _queue_messages(conn)
    for score in top[:5]:
        messages += stats.sample_messages(conn, start, now, theme_id=score.theme_id, limit=4)
    messages += stats.sample_messages(conn, start, now, kinds=("praise",), limit=8, most_negative=False)
    messages += stats.sample_messages(conn, start, now, limit=10)
    return {
        "kind": "weekly",
        "period": {"start": to_iso(start), "end": to_iso(now)},
        "current": stats.period_summary(conn, start, now),
        "previous": stats.period_summary(conn, previous_start, start),
        "top_themes": [asdict(s) for s in top],
        "mod_queue": stats.queue_counts(conn),
        "messages": _dedupe(messages),
    }


def _keyword_messages(conn, start, end, keywords, limit=30) -> list[dict]:
    if not keywords or end <= start:
        return []
    clause = " OR ".join("lower(m.content) LIKE ? ESCAPE '\\'" for _ in keywords)
    rows = conn.execute(
        f"SELECT {stats.MESSAGE_COLUMNS} FROM messages m JOIN triage t ON t.message_id = m.id"
        " WHERE m.is_team = 0 AND m.is_bot = 0 AND m.created_at >= ? AND m.created_at < ?"
        f" AND ({clause}) ORDER BY t.sentiment ASC, m.created_at DESC, m.id LIMIT ?",
        [to_iso(start), to_iso(end), *[stats._like(k.lower()) for k in keywords], limit],
    ).fetchall()
    return [stats.to_message(r) for r in rows]


def _days(start: datetime, end: datetime) -> float:
    return round((end - start).total_seconds() / 86400, 2)


def build_launch_input(conn: sqlite3.Connection, launch: sqlite3.Row, now: datetime) -> dict:
    """After = [launch day, min(day + 14 days, now)); before = the equal-length span just before the day."""
    day = datetime.combine(date.fromisoformat(launch["date"]), time.min, timezone.utc)
    if day > now:
        raise LookupError(f"launch {launch['name']} is in the future ({launch['date']}); nothing to compare yet")
    after_end = min(day + timedelta(days=LAUNCH_WINDOW_DAYS), now)
    before_start = day - (after_end - day)
    keywords = json.loads(launch["keywords"])
    messages = _keyword_messages(conn, day, after_end, keywords)
    if after_end > day:
        messages += stats.sample_messages(conn, day, after_end, kinds=("praise",), limit=8, most_negative=False)
        messages += stats.sample_messages(conn, day, after_end, limit=10)
    return {
        "kind": "launch",
        "launch": {"name": launch["name"], "date": launch["date"], "keywords": keywords},
        "before": {**stats.period_summary(conn, before_start, day), "days": _days(before_start, day)},
        "after": {**stats.period_summary(conn, day, after_end), "days": _days(day, after_end)},
        "top_themes_after": [asdict(s) for s in stats.theme_scores(conn, after_end, start=day, limit=10)],
        "messages": _dedupe(messages),
    }


def _require_text(data: dict) -> None:
    if not data["markdown"].strip():
        raise ValueError("digest markdown is empty")


def run_digest(
    conn: sqlite3.Connection, llm: LLMClient, now: datetime, *, launch: str | None = None
) -> DigestResult:
    launch_id = None
    if launch is None:
        data = build_weekly_input(conn, now)
        period = (data["period"]["start"], data["period"]["end"])
        activity = data["current"]["messages"]
    else:
        row = conn.execute("SELECT * FROM launches WHERE name = ?", (launch,)).fetchone()
        if row is None:
            raise LookupError(f"unknown launch {launch!r}; add it under [[launches]] in pulse.toml")
        launch_id = row["id"]
        data = build_launch_input(conn, row, now)
        period = (data["before"]["start"], data["after"]["end"])
        activity = data["before"]["messages"] + data["after"]["messages"]

    if activity == 0:
        return _save(conn, data["kind"], period, launch_id, EMPTY_MARKDOWN, [], None, now)

    allowed = {m["message_id"] for m in data["messages"]}
    resp = llm.complete(
        "digest", load_prompt(), json.dumps(data, ensure_ascii=False), DIGEST_SCHEMA, SCHEMA_NAME,
        validate=_require_text,
    )
    markdown, removed = strip_unknown(resp.data["markdown"], allowed)
    return _save(conn, data["kind"], period, launch_id, markdown, removed, resp.run_id, now)


def _save(conn, kind, period, launch_id, markdown, removed, run_id, now) -> DigestResult:
    cited = cited_ids(markdown)
    with conn:
        digest_id = int(conn.execute(
            "INSERT INTO digests (kind, period_start, period_end, launch_id, markdown, cited_message_ids,"
            " run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, period[0], period[1], launch_id, markdown, json.dumps(cited), run_id, to_iso(now)),
        ).lastrowid)
    return DigestResult(digest_id, kind, period[0], period[1], markdown, cited, removed)
