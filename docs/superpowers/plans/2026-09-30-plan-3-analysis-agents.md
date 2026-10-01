# Discord Pulse Plan 3: Analysis Agents Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn triaged messages into recurring themes (pain points), weekly and launch digests that cite real messages, and an Investigate agent that answers "why" questions with read-only tools, all runnable from the CLI.

**Architecture:** A read-only `stats` module feeds every agent. Themes are assigned in two stages like triage: Jev picks an existing theme when it is confident, and the theme LLM proposes new themes, merges and renames for the rest; plain code applies proposals with guardrails and an event log. The digest agent gets precomputed stats plus sample messages and must cite with `[[msg:<id>]]`; citations to messages it was not shown are stripped. Investigate runs a provider-neutral tool loop (`LLMClient.run_tools`) over three read-only tools.

**Tech Stack:** Python 3.12, sqlite3, anthropic + openai SDKs (tool calling), httpx (Jev), pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-discord-pulse-design.md` (sections 4, 5, 6.2-6.4, 9; 14.1 for Jev theme assignment).

**Plan series:** Plans 1-2 done. Plan 3 (this) = analysis agents. Plan 4 = dashboard (Bugs view) + seed-demo. Plan 5 = eval harness (Jev gate), bot adapter, bot pitch, launchd.

## Global Constraints

- Python `>=3.12`; run tests with `.venv/bin/pytest`; tests never touch the network (fakes only).
- All stored timestamps come from `pulse.models.to_iso` (fixed-width UTC).
- Secrets only from env vars. Agent modules never import provider SDKs or httpx; they go through `LLMClient` (`complete`, `classify`, `choose`, `run_tools`).
- When a thread pool is running, DB writes happen on the calling thread under `with llm.db_lock, conn:`.
- Every commit message ends with a blank line then exactly `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Citations in agent markdown use `[[msg:<message_id>]]`. Stored `cited_message_ids` only ever contain ids the agent was shown.
- Theme guardrails (spec 6.2): at most 5 new themes and 3 merges per theme run; a merge needs both themes active and different; theme history is never rewritten (merges set `status='merged'`, `merged_into`; reads resolve to the active root).
- Pain point score (spec 6): `score = volume_7d * mean_negativity * (1 + max(0, trend))`, `mean_negativity = mean(max(0, -sentiment))`, `trend = (volume_7d - volume_prev_7d) / max(volume_prev_7d, 1)`.
- Spec clarifications made by this plan (treat as spec): `triage.themed_at` marks messages the theme stage has processed; only non-staff messages with non-empty `topics` are theme candidates (Jev-only rows have no topics); Jev theme assignment reuses `[classifier] min_confidence` and offers at most 40 themes; the theme LLM runs sequential batches of 60 so each batch sees the themes the previous one created; Investigate is synchronous in the CLI and stores a row before running (markdown filled on success, a failure note on error); at most 12 tool calls per investigation.

## Review Focus

1. Messages tagged with a theme that was later merged must count toward the surviving theme in scores and samples, exactly once (Task 2 `test_merged_theme_counts_toward_target_once`).
2. A theme proposal naming theme ids or message ids outside its input is retried once and then leaves the batch unthemed with nothing applied (Task 5 `test_invalid_proposal_is_retried_then_left_unthemed`).
3. An agent citing a message it was never shown gets that citation stripped and reported, never stored (Task 6 `test_weekly_digest_strips_unknown_citations`, Task 9 `test_investigation_strips_citations_to_unseen_messages`).
4. Bad tool arguments from the model (unknown tool, bad date, huge limit, missing message) return an error string or a clamped result to the model and never crash the run (Task 9 `test_toolbox_rejects_bad_arguments`, `test_search_limit_is_clamped`).
5. A single theme run proposing more than 5 new themes or 3 merges is capped with each rejection reported (Task 3 `test_apply_proposal_enforces_run_limits`).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `pulse/db.py` | `triage.themed_at` column, `message_themes` theme index | 1 |
| `pulse/store.py` | `sync_launches` | 1 |
| `pulse/citations.py` | `[[msg:id]]` parsing, stripping, text rendering | 1 |
| `pulse/stats.py` | read-only aggregates and message samples | 2 |
| `pulse/themes.py` | theme store + `apply_proposal` guardrails | 3 |
| `pulse/agents/classifier.py`, `pulse/agents/providers/jev_backend.py`, `pulse/agents/llm.py` | Jev `choose` + `LLMClient.choose` | 4 |
| `pulse/agents/theme.py`, `pulse/agents/prompts/theme_v1.md` | theme agent | 5 |
| `pulse/agents/digest.py`, `pulse/agents/prompts/digest_v1.md` | digest agent | 6 |
| `pulse/agents/base.py`, `pulse/agents/llm.py` | tool-calling contract, `LLMClient.run_tools` | 7 |
| `pulse/agents/providers/anthropic_backend.py`, `openai_backend.py` | `tool_step` | 8 |
| `pulse/agents/investigate.py`, `pulse/agents/prompts/investigate_v1.md` | Investigate agent + toolbox | 9 |
| `pulse/pipeline.py`, `pulse/run.py`, `README.md` | CLI commands, pipeline wiring, docs | 10 |
| `tests/fakes.py` | `set_triage(topics=)`, `FakeClassifier.choose`, `FakeBackend.tool_step` | 3, 4, 7 |

---

### Task 1: Themed-at column, launch sync, citations

**Files:**
- Modify: `pulse/db.py`, `pulse/store.py`
- Create: `pulse/citations.py`
- Test: `tests/test_db.py` (append), `tests/test_store.py` (append), `tests/test_citations.py`

**Interfaces:**
- Produces:
  - `triage.themed_at TEXT` (nullable; added to the `CREATE TABLE` and to `_TRIAGE_ADDED_COLUMNS`), index `idx_message_themes_theme ON message_themes(theme_id)`.
  - `sync_launches(conn, launches: Iterable[Launch]) -> int` (upsert by name; returns the number synced).
  - `pulse/citations.py`: `CITATION_RE`, `cited_ids(markdown: str) -> list[str]` (unique, in order), `strip_unknown(markdown: str, allowed: set[str]) -> tuple[str, list[str]]` (removes tokens whose id is not allowed; returns cleaned text and removed ids in order), `render_text(markdown: str, conn) -> str` (each token becomes `(author, https://discord.com/channels/g/c/m)`, unknown ids become `[missing message]`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
def test_triage_has_themed_at_column():
    assert "themed_at" in triage_columns(connect(":memory:"))


def test_old_db_gains_themed_at(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()
    conn = connect(path)
    assert "themed_at" in triage_columns(conn)
    assert conn.execute("SELECT themed_at FROM triage WHERE message_id = 'm'").fetchone()[0] is None
```

Append to `tests/test_store.py`:

```python
import json

from pulse.config import Launch
from pulse.store import sync_launches


def test_sync_launches_upserts_by_name():
    conn = connect(":memory:")
    assert sync_launches(conn, [Launch("v2.0 SDK", "2026-09-15", ("v2", "migration"))]) == 1
    sync_launches(conn, [Launch("v2.0 SDK", "2026-09-16", ("v2",)), Launch("CLI 3", "2026-10-01", ())])
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM launches")}
    assert set(rows) == {"v2.0 SDK", "CLI 3"}
    assert rows["v2.0 SDK"]["date"] == "2026-09-16"
    assert json.loads(rows["v2.0 SDK"]["keywords"]) == ["v2"]
```

Create `tests/test_citations.py`:

```python
from pulse.citations import cited_ids, render_text, strip_unknown
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import msg


def test_cited_ids_unique_in_order():
    md = "A [[msg:2]] then [[msg:1]] and again [[msg:2]]."
    assert cited_ids(md) == ["2", "1"]


def test_strip_unknown_removes_and_reports():
    cleaned, removed = strip_unknown("Good [[msg:1]] bad [[msg:x9]] ok.", {"1"})
    assert cleaned == "Good [[msg:1]] bad  ok."
    assert removed == ["x9"]


def test_render_text_links_known_and_marks_missing():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", "hi", author_name="alice")], frozenset())
    text = render_text("See [[msg:m1]] and [[msg:gone]].", conn)
    assert text == "See (alice, https://discord.com/channels/900/100/m1) and [missing message]."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py tests/test_store.py tests/test_citations.py -v`
Expected: FAIL (`themed_at` missing; `ImportError` for `sync_launches` and `pulse.citations`)

- [ ] **Step 3: Implement**

`pulse/db.py`: add `themed_at TEXT` as the last column of the `triage` `CREATE TABLE`; append `("themed_at", "TEXT")` to `_TRIAGE_ADDED_COLUMNS`; add to `SCHEMA` after the `message_themes` table:

```sql
CREATE INDEX IF NOT EXISTS idx_message_themes_theme ON message_themes(theme_id);
```

`pulse/store.py` — add `import json`, `from pulse.config import Launch`, and:

```python
def sync_launches(conn: sqlite3.Connection, launches: Iterable[Launch]) -> int:
    """Mirror [[launches]] from the config into the launches table, keyed by name."""
    count = 0
    with conn:
        for launch in launches:
            conn.execute(
                "INSERT INTO launches (name, date, keywords) VALUES (?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET date = excluded.date, keywords = excluded.keywords",
                (launch.name, launch.date, json.dumps(list(launch.keywords))),
            )
            count += 1
    return count
```

Create `pulse/citations.py`:

```python
"""Message citations in agent markdown: [[msg:<message_id>]]."""
from __future__ import annotations

import re
import sqlite3

from pulse.links import jump_link

CITATION_RE = re.compile(r"\[\[msg:([^\]\s]+)\]\]")


def cited_ids(markdown: str) -> list[str]:
    seen: list[str] = []
    for mid in CITATION_RE.findall(markdown):
        if mid not in seen:
            seen.append(mid)
    return seen


def strip_unknown(markdown: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Remove citations to messages the agent was not shown."""
    removed: list[str] = []

    def replace(match: re.Match) -> str:
        mid = match.group(1)
        if mid in allowed:
            return match.group(0)
        removed.append(mid)
        return ""

    return CITATION_RE.sub(replace, markdown), removed


def render_text(markdown: str, conn: sqlite3.Connection) -> str:
    """Plain-text rendering for the CLI: each citation becomes the author and a jump link."""

    def replace(match: re.Match) -> str:
        row = conn.execute(
            "SELECT guild_id, channel_id, author_name FROM messages WHERE id = ?", (match.group(1),)
        ).fetchone()
        if row is None:
            return "[missing message]"
        return f"({row['author_name']}, {jump_link(row['guild_id'], row['channel_id'], match.group(1))})"

    return CITATION_RE.sub(replace, markdown)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py tests/test_store.py tests/test_citations.py -v` — Expected: all pass.

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/db.py pulse/store.py pulse/citations.py tests/test_db.py tests/test_store.py tests/test_citations.py
git commit -m "feat: themed_at column, launch sync, message citation helpers" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Read-only stats

**Files:**
- Create: `pulse/stats.py`
- Test: `tests/test_stats.py`

**Interfaces:**
- Consumes: `KINDS`, `to_iso` (models); schema from Plan 1 plus Task 1.
- Produces (all read-only; windows are `[start, end)` in UTC; only non-team, non-bot, triaged messages count):
  - `theme_resolution(conn) -> dict[int, int]` (every theme id → its active root, following `merged_into`, cycle-safe)
  - `theme_member_ids(conn, theme_id: int) -> list[int]` (all ids resolving to the same root; `[]` if unknown)
  - `period_summary(conn, start, end) -> dict` keys `start, end, messages, avg_sentiment, negative, needs_reply, by_kind`
  - `sentiment_series(conn, start, end) -> list[dict]` one row per UTC day: `day, messages, avg_sentiment`
  - `@dataclass(frozen=True) ThemeScore(theme_id, name, description, volume, prev_volume, mean_negativity, trend, score, kinds)`
  - `theme_scores(conn, now, *, window_days=7, limit=20) -> list[ThemeScore]` (active themes with volume > 0, highest score first)
  - `queue_counts(conn) -> dict` keys `open, frustrated, unanswered`
  - `MESSAGE_COLUMNS: str`, `to_message(row) -> dict` keys `message_id, author, channel, created_at, kind, sentiment, content` (content clipped to 500 chars)
  - `sample_messages(conn, start, end, *, theme_id=None, kinds=None, limit=10, most_negative=True) -> list[dict]`

- [ ] **Step 1: Write the failing tests** — `tests/test_stats.py`:

```python
from datetime import timedelta

import pytest

from pulse import stats
from pulse.db import connect
from pulse.models import to_iso
from pulse.store import upsert_messages
from tests.fakes import T0, msg, set_triage

DAY = 24 * 60
NOW = T0 + timedelta(days=1)


def db(*items):
    """items: (message, sentiment, kind)"""
    conn = connect(":memory:")
    upsert_messages(conn, [m for m, _, _ in items], frozenset({"t1"}))
    for m, sentiment, kind in items:
        set_triage(conn, m.id, sentiment=sentiment, kind=kind)
    return conn


def theme(conn, name, *message_ids, status="active", merged_into=None):
    with conn:
        tid = conn.execute(
            "INSERT INTO themes (name, description, status, merged_into, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, f"{name} desc", status, merged_into, to_iso(T0)),
        ).lastrowid
        conn.executemany("INSERT INTO message_themes (message_id, theme_id) VALUES (?, ?)", [(m, tid) for m in message_ids])
    return tid


def test_period_summary_counts_only_community_messages():
    conn = db(
        (msg("a", minutes=0), -2, "bug"),
        (msg("b", minutes=1), 1, "praise"),
        (msg("s", minutes=2, author_id="t1"), 0, "other"),
    )
    s = stats.period_summary(conn, T0 - timedelta(hours=1), NOW)
    assert (s["messages"], s["negative"], s["avg_sentiment"]) == (2, 1, -0.5)
    assert s["by_kind"]["bug"] == 1 and s["by_kind"]["praise"] == 1 and s["by_kind"]["other"] == 0


def test_sentiment_series_fills_empty_days():
    conn = db((msg("a", minutes=0), -1, "bug"), (msg("b", minutes=2 * DAY), 1, "praise"))
    series = stats.sentiment_series(conn, T0.replace(hour=0), (T0 + timedelta(days=3)).replace(hour=0))
    assert [r["day"] for r in series] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert [r["messages"] for r in series] == [1, 0, 1]
    assert series[1]["avg_sentiment"] is None


def test_theme_score_formula():
    conn = db(
        (msg("c1", minutes=0), -2, "bug"),
        (msg("c2", minutes=10), -1, "bug"),
        (msg("c3", minutes=20), 0, "question"),
        (msg("p1", minutes=-8 * DAY), -1, "bug"),
    )
    theme(conn, "Install", "c1", "c2", "c3", "p1")
    [s] = stats.theme_scores(conn, NOW)
    assert (s.volume, s.prev_volume) == (3, 1)
    assert s.mean_negativity == pytest.approx(1.0)
    assert s.trend == pytest.approx(2.0)
    assert s.score == pytest.approx(9.0)
    assert s.kinds == {"bug": 2, "question": 1}


def test_merged_theme_counts_toward_target_once():
    conn = db((msg("a", minutes=0), -2, "bug"), (msg("b", minutes=5), -2, "bug"))
    target = theme(conn, "Auth docs", "a")
    theme(conn, "Token exchange", "a", "b", status="merged", merged_into=target)
    [s] = stats.theme_scores(conn, NOW)
    assert (s.theme_id, s.name, s.volume) == (target, "Auth docs", 2)
    assert {m["message_id"] for m in stats.sample_messages(conn, T0 - timedelta(days=1), NOW, theme_id=target)} == {"a", "b"}


def test_theme_resolution_survives_cycles():
    conn = connect(":memory:")
    a = theme(conn, "A")
    b = theme(conn, "B", status="merged", merged_into=a)
    with conn:
        conn.execute("UPDATE themes SET status = 'merged', merged_into = ? WHERE id = ?", (b, a))
    resolved = stats.theme_resolution(conn)
    assert set(resolved) == {a, b}


def test_themes_with_no_recent_volume_are_excluded():
    conn = db((msg("old", minutes=-9 * DAY), -2, "bug"))
    theme(conn, "Old", "old")
    assert stats.theme_scores(conn, NOW) == []


def test_queue_counts():
    conn = db((msg("a"), -2, "bug"), (msg("b", minutes=1), 0, "question"))
    with conn:
        conn.execute("INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('a','a','frustrated','open','x')")
        conn.execute("INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('b','b','unanswered','open','x')")
        conn.execute("INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('c','b','unanswered','handled','x')")
    assert stats.queue_counts(conn) == {"open": 2, "frustrated": 1, "unanswered": 1}


def test_sample_messages_order_and_filters():
    conn = db(
        (msg("n", "ugh", minutes=0), -2, "bug"),
        (msg("p", "love it", minutes=1), 2, "praise"),
        (msg("q", "how?", minutes=2), 0, "question"),
    )
    start, end = T0 - timedelta(hours=1), NOW
    assert [m["message_id"] for m in stats.sample_messages(conn, start, end)] == ["n", "q", "p"]
    assert [m["message_id"] for m in stats.sample_messages(conn, start, end, most_negative=False)] == ["p", "q", "n"]
    assert [m["message_id"] for m in stats.sample_messages(conn, start, end, kinds=("praise",))] == ["p"]
    first = stats.sample_messages(conn, start, end, limit=1)[0]
    assert set(first) == {"message_id", "author", "channel", "created_at", "kind", "sentiment", "content"}
    assert stats.sample_messages(conn, start, end, theme_id=999) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stats.py -v` — Expected: FAIL with `ImportError: cannot import name 'stats'`

- [ ] **Step 3: Implement** `pulse/stats.py`:

```python
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
    conn: sqlite3.Connection, now: datetime, *, window_days: int = 7, limit: int = 20
) -> list[ThemeScore]:
    cur_start = now - timedelta(days=window_days)
    prev_start = cur_start - timedelta(days=window_days)
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stats.py -v` — Expected: 8 passed.

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/stats.py tests/test_stats.py
git commit -m "feat: read-only stats for periods, sentiment series, theme scores and samples" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Theme store and proposal guardrails

**Files:**
- Create: `pulse/themes.py`
- Modify: `tests/fakes.py` (`set_triage` gains `topics=()`)
- Test: `tests/test_themes.py`

**Interfaces:**
- Consumes: `theme_resolution` (Task 2), `to_iso`.
- Produces:
  - `MAX_NEW_THEMES_PER_RUN = 5`, `MAX_MERGES_PER_RUN = 3`
  - `active_themes(conn) -> list[sqlite3.Row]` (`id, name, description`, ordered by id)
  - `find_active(conn, name) -> int | None` (case- and whitespace-insensitive)
  - `create_theme(conn, name, description, now, run_id=None) -> tuple[int, bool]` (returns existing active theme with the same name and `False`, else creates, logs a `create` event, returns `True`; raises `ValueError` on an empty name)
  - `assign(conn, message_id, theme_id) -> bool`
  - `merge_themes(conn, from_id, into_id, now, run_id=None, reason="") -> None`
  - `rename_theme(conn, theme_id, name, description, now, run_id=None) -> None`
  - `mark_themed(conn, message_ids, now) -> None`
  - `@dataclass ThemeBudget(new_themes_left=5, merges_left=3)`; `@dataclass ThemeChanges(created=0, assigned=0, merged=0, renamed=0, rejected=list)`
  - `apply_proposal(conn, proposal: dict, now, run_id, budget: ThemeBudget) -> ThemeChanges`. Proposal keys: `assignments[{message_id, theme_ids}]`, `new_themes[{name, description, message_ids}]`, `merges[{from_id, into_id, reason}]`, `renames[{theme_id, name, description}]`. Applied in order renames → merges → new themes → assignments. Assignments to a merged theme go to its active root. The caller wraps the call in a transaction.
  - `tests/fakes.set_triage(..., topics=())`

- [ ] **Step 1: Extend `set_triage` in `tests/fakes.py`** — replace it with:

```python
def set_triage(
    conn, message_id: str, *, sentiment: int = 0, needs_reply: bool = False, kind: str = "question", topics=()
) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO triage (message_id, sentiment, confidence, kind, topics,"
            " needs_reply, prompt_version, created_at) VALUES (?, ?, 0.9, ?, ?, ?, 'test', ?)",
            (message_id, sentiment, kind, json.dumps(list(topics)), int(needs_reply), to_iso(T0)),
        )
```

- [ ] **Step 2: Write the failing tests** — `tests/test_themes.py`:

```python
import json

from pulse.db import connect
from pulse.store import upsert_messages
from pulse.themes import (
    ThemeBudget, active_themes, apply_proposal, assign, create_theme, find_active, mark_themed, merge_themes,
    rename_theme,
)
from tests.fakes import T0, msg, set_triage


def db(*ids):
    conn = connect(":memory:")
    upsert_messages(conn, [msg(i, minutes=n) for n, i in enumerate(ids)], frozenset())
    for i in ids:
        set_triage(conn, i, sentiment=-1, kind="bug", topics=["install"])
    return conn


def events(conn):
    return [(r["kind"], json.loads(r["payload"])) for r in conn.execute("SELECT * FROM theme_events ORDER BY id")]


def empty(**overrides):
    base = {"assignments": [], "new_themes": [], "merges": [], "renames": []}
    base.update(overrides)
    return base


def test_create_theme_dedupes_case_insensitively():
    conn = db()
    with conn:
        tid, created = create_theme(conn, "Auth  Docs", "confusing auth guide", T0)
        again, created_again = create_theme(conn, "auth docs", "other", T0)
    assert created and not created_again and again == tid
    assert find_active(conn, " AUTH docs ") == tid
    assert [k for k, _ in events(conn)] == ["create"]


def test_merge_and_rename_log_events():
    conn = db()
    with conn:
        a, _ = create_theme(conn, "A", "a", T0)
        b, _ = create_theme(conn, "B", "b", T0)
        merge_themes(conn, b, a, T0, run_id=None, reason="same thing")
        rename_theme(conn, a, "A2", "a2", T0)
    assert [r["id"] for r in active_themes(conn)] == [a]
    assert tuple(conn.execute("SELECT status, merged_into FROM themes WHERE id = ?", (b,)).fetchone()) == ("merged", a)
    kinds = [k for k, _ in events(conn)]
    assert kinds == ["create", "create", "merge", "rename"]
    assert events(conn)[3][1] == {"theme_id": a, "old_name": "A", "name": "A2", "old_description": "a", "description": "a2"}


def test_assign_is_idempotent_and_mark_themed_sets_timestamp():
    conn = db("m1")
    with conn:
        tid, _ = create_theme(conn, "A", "a", T0)
        assert assign(conn, "m1", tid) is True
        assert assign(conn, "m1", tid) is False
        mark_themed(conn, ["m1"], T0)
    assert conn.execute("SELECT themed_at FROM triage WHERE message_id = 'm1'").fetchone()[0] == "2026-09-28T12:00:00.000000Z"


def test_apply_proposal_full_flow():
    conn = db("m1", "m2", "m3")
    with conn:
        old, _ = create_theme(conn, "Old name", "x", T0)
        dup, _ = create_theme(conn, "Dup", "y", T0)
        changes = apply_proposal(conn, {
            "renames": [{"theme_id": old, "name": "M1 install", "description": "arm64 wheels"}],
            "merges": [{"from_id": dup, "into_id": old, "reason": "same issue"}],
            "new_themes": [{"name": "Auth docs", "description": "token step missing", "message_ids": ["m2"]}],
            "assignments": [{"message_id": "m1", "theme_ids": [old]}, {"message_id": "m3", "theme_ids": [dup]}],
        }, T0, None, ThemeBudget())
    assert (changes.renamed, changes.merged, changes.created, changes.assigned, changes.rejected) == (1, 1, 1, 3, [])
    by_msg = dict(conn.execute("SELECT message_id, theme_id FROM message_themes").fetchall())
    assert by_msg["m1"] == old and by_msg["m3"] == old  # m3 went to the merge target
    assert conn.execute("SELECT name FROM themes WHERE id = ?", (by_msg["m2"],)).fetchone()[0] == "Auth docs"


def test_apply_proposal_enforces_run_limits():
    conn = db("m1")
    budget = ThemeBudget()
    with conn:
        ids = [create_theme(conn, f"T{i}", "", T0)[0] for i in range(5)]
        changes = apply_proposal(conn, empty(
            new_themes=[{"name": f"New {i}", "description": "", "message_ids": []} for i in range(6)],
            merges=[{"from_id": ids[i], "into_id": ids[4], "reason": ""} for i in range(4)],
        ), T0, None, budget)
    assert (changes.created, changes.merged) == (5, 3)
    assert len(changes.rejected) == 2
    assert any("5 new themes" in r for r in changes.rejected)
    assert any("3 merges" in r for r in changes.rejected)
    assert (budget.new_themes_left, budget.merges_left) == (0, 0)


def test_new_theme_matching_existing_name_reuses_it_without_budget():
    conn = db("m1")
    budget = ThemeBudget(new_themes_left=0)
    with conn:
        tid, _ = create_theme(conn, "Rate limits", "", T0)
        changes = apply_proposal(conn, empty(
            new_themes=[{"name": "rate LIMITS", "description": "", "message_ids": ["m1"]}]
        ), T0, None, budget)
    assert (changes.created, changes.assigned, changes.rejected) == (0, 1, [])
    assert conn.execute("SELECT theme_id FROM message_themes").fetchone()[0] == tid


def test_invalid_references_are_rejected_not_applied():
    conn = db("m1")
    with conn:
        tid, _ = create_theme(conn, "A", "", T0)
        changes = apply_proposal(conn, empty(
            renames=[{"theme_id": 999, "name": "X", "description": ""}],
            merges=[{"from_id": tid, "into_id": tid, "reason": ""}],
            assignments=[{"message_id": "m1", "theme_ids": [999]}],
        ), T0, None, ThemeBudget())
    assert (changes.renamed, changes.merged, changes.assigned) == (0, 0, 0)
    assert len(changes.rejected) == 3
    assert conn.execute("SELECT COUNT(*) FROM message_themes").fetchone()[0] == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_themes.py -v` — Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.themes'`

- [ ] **Step 4: Implement** `pulse/themes.py`:

```python
"""Theme store: create, assign, merge, rename, with an append-only event log.

History is never rewritten: a merge marks the source theme merged and points it
at the target; reads resolve merged themes to their active root.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
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


@dataclass
class ThemeChanges:
    created: int = 0
    assigned: int = 0
    merged: int = 0
    renamed: int = 0
    rejected: list[str] = field(default_factory=list)


def apply_proposal(
    conn: sqlite3.Connection, proposal: dict, now: datetime, run_id: int | None, budget: ThemeBudget
) -> ThemeChanges:
    changes = ThemeChanges()
    active = {r["id"] for r in active_themes(conn)}

    for r in proposal["renames"]:
        if r["theme_id"] not in active or not _clean(r["name"]):
            changes.rejected.append(f"rename {r['theme_id']}: not an active theme or empty name")
            continue
        rename_theme(conn, r["theme_id"], r["name"], r["description"], now, run_id)
        changes.renamed += 1

    for m in proposal["merges"]:
        src, dst = m["from_id"], m["into_id"]
        if src == dst or src not in active or dst not in active:
            changes.rejected.append(f"merge {src}->{dst}: both themes must be active and different")
            continue
        if budget.merges_left <= 0:
            changes.rejected.append(f"merge {src}->{dst}: run limit of {MAX_MERGES_PER_RUN} merges reached")
            continue
        merge_themes(conn, src, dst, now, run_id, m["reason"])
        active.discard(src)
        budget.merges_left -= 1
        changes.merged += 1

    for t in proposal["new_themes"]:
        if not _clean(t["name"]):
            changes.rejected.append("new theme with an empty name")
            continue
        tid = find_active(conn, t["name"])
        if tid is None:
            if budget.new_themes_left <= 0:
                changes.rejected.append(
                    f"new theme {t['name']!r}: run limit of {MAX_NEW_THEMES_PER_RUN} new themes reached"
                )
                continue
            tid, _ = create_theme(conn, t["name"], t["description"], now, run_id)
            budget.new_themes_left -= 1
            changes.created += 1
            active.add(tid)
        for mid in t["message_ids"]:
            changes.assigned += assign(conn, mid, tid)

    resolved = theme_resolution(conn)
    for a in proposal["assignments"]:
        for tid in a["theme_ids"]:
            root = resolved.get(tid)
            if root is None or root not in active:
                changes.rejected.append(f"assign {a['message_id']}->{tid}: not an active theme")
                continue
            changes.assigned += assign(conn, a["message_id"], root)
    return changes
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_themes.py -v` — Expected: 7 passed.

- [ ] **Step 6: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/themes.py tests/fakes.py tests/test_themes.py
git commit -m "feat: theme store with event log and proposal guardrails" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Jev theme choice and `LLMClient.choose`

**Files:**
- Modify: `pulse/agents/classifier.py`, `pulse/agents/providers/jev_backend.py`, `pulse/agents/llm.py`, `tests/fakes.py`
- Test: `tests/test_jev_backend.py` (append), `tests/test_llm_classify.py` (append)

**Interfaces:**
- Produces:
  - `classifier.py`: `@dataclass(frozen=True) ChoiceResult(choice: str, confidence: float, reported_cost: float | None = None)`; `theme_question(themes) -> dict` (a choice question whose criteria are `str(theme id) -> "name: description"` plus `"none"`); `parse_choice(data: dict, question: dict) -> ChoiceResult` (raises on a choice not offered or confidence outside [0, 1]); `Classifier` protocol gains `choose(self, model: str, state: dict, question: dict) -> ChoiceResult`.
  - `JevBackend.choose(model, state, question)` posting `questions={"choice": question}`; shared private `_post` for the HTTP call, status mapping, JSON parsing and reported cost.
  - `llm.py`: `@dataclass(frozen=True) ChoiceResponse(result: ChoiceResult, run_id: int)`; `LLMClient.choose(state, question) -> ChoiceResponse` with the same contract as `classify` (budget gate, backoff, one retry on invalid output, one `agent_runs` row with agent `classifier`). `classify` and `choose` share a private `_classifier_call`.
  - `tests/fakes.py`: `FakeClassifier(responses=None, handler=None, choices=None, choose_handler=None)` with `.choose_calls`; `choose_handler(state, question)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_jev_backend.py`:

```python
from pulse.agents.classifier import ChoiceResult, theme_question

THEMES = [{"id": 3, "name": "Auth docs", "description": "token step missing"},
          {"id": 7, "name": "M1 install", "description": "arm64 wheels"}]
CHOICE_FIXTURE = {
    "model": "typesafe/jev-1.13-20260917",
    "answers": {"choice": {"type": "choice", "choice": "7", "probabilities": {"3": 0.05, "7": 0.9, "none": 0.05}, "confidence": 0.9}},
    "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 1.2e-05},
}


def test_theme_question_offers_each_theme_and_none():
    q = theme_question(THEMES)
    assert q["type"] == "choice"
    assert set(q["criteria"]) == {"3", "7", "none"}
    assert q["criteria"]["7"] == "M1 install: arm64 wheels"


def test_choose_posts_single_choice_question_and_parses():
    backend, seen = backend_with(lambda r: httpx.Response(200, json=CHOICE_FIXTURE))
    q = theme_question(THEMES)
    result = backend.choose("jev-latest", STATE, q)
    assert result == ChoiceResult(choice="7", confidence=0.9, reported_cost=pytest.approx(1.2e-05))
    assert json.loads(seen[0].content)["questions"] == {"choice": q}


def test_choose_rejects_a_choice_not_offered():
    bad = json.loads(json.dumps(CHOICE_FIXTURE))
    bad["answers"]["choice"]["choice"] = "42"
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid):
        backend.choose("jev-latest", STATE, theme_question(THEMES))


def test_choose_maps_server_errors_to_transient():
    backend, _ = backend_with(lambda r: httpx.Response(503, text="busy"))
    with pytest.raises(TransientError):
        backend.choose("jev-latest", STATE, theme_question(THEMES))
```

Append to `tests/test_llm_classify.py`:

```python
from pulse.agents.classifier import ChoiceResult

QUESTION = {"type": "choice", "criteria": {"1": "A", "none": "none"}}


def test_choose_returns_choice_and_logs_classifier_run():
    conn = connect(":memory:")
    fc = FakeClassifier(choices=[ChoiceResult("1", 0.8, 0.00001)])
    llm = make_llm(conn, make_config(classifier=classifier_config()), FakeBackend(), classifier=fc)
    resp = llm.choose({"message_id": "m1"}, QUESTION)
    assert (resp.result.choice, resp.result.confidence) == ("1", 0.8)
    [run] = runs(conn)
    assert (run["agent"], run["status"]) == ("classifier", "ok")
    assert run["cost_usd"] == pytest.approx(0.00001)
    assert fc.choose_calls == [{"model": "jev-latest", "state": {"message_id": "m1"}, "question": QUESTION}]


def test_choose_is_blocked_by_budget_cap():
    conn = connect(":memory:")
    fc = FakeClassifier(choices=[ChoiceResult("1", 0.8)])
    llm = make_llm(conn, make_config(classifier=classifier_config(), daily_usd_cap=0.0), FakeBackend(), classifier=fc)
    with pytest.raises(BudgetExceeded):
        llm.choose({}, QUESTION)
    assert fc.choose_calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_jev_backend.py tests/test_llm_classify.py -v`
Expected: FAIL with `ImportError: cannot import name 'ChoiceResult'`

- [ ] **Step 3: Implement**

`pulse/agents/classifier.py` — add after `ClassifierResult`:

```python
@dataclass(frozen=True)
class ChoiceResult:
    choice: str
    confidence: float
    reported_cost: float | None = None
```

add `choose` to the protocol:

```python
class Classifier(Protocol):
    def classify(self, model: str, state: dict) -> ClassifierResult: ...

    def choose(self, model: str, state: dict, question: dict) -> ChoiceResult: ...
```

and append:

```python
def theme_question(themes) -> dict:
    """A single choice question over existing themes (rows or dicts with id, name, description)."""
    criteria = {str(t["id"]): f"{t['name']}: {t['description']}" for t in themes}
    criteria["none"] = "None of these themes fits this message."
    return {
        "type": "choice",
        "instructions": "Which recurring community theme is this Discord message about?",
        "criteria": criteria,
    }


def parse_choice(data: dict, question: dict) -> ChoiceResult:
    answer = data["answers"]["choice"]
    choice = str(answer["choice"])
    confidence = float(answer["confidence"])
    if choice not in question["criteria"]:
        raise ValueError(f"choice {choice!r} was not offered")
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"confidence out of range: {confidence}")
    return ChoiceResult(choice=choice, confidence=confidence)
```

`pulse/agents/providers/jev_backend.py` — replace the body of the class after `__init__` with a shared `_post` plus the two public methods (keep the existing error mapping and cost parsing, now in `_post`):

```python
    def _post(self, model: str, state: dict, questions: dict) -> tuple[dict, float | None]:
        try:
            resp = self._client.post(
                self._url,
                json={"model": model, "state": state, "questions": questions},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TransportError as e:
            raise TransientError(str(e)) from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"{resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"{resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise OutputInvalid(f"not JSON: {e}") from e
        reported: float | None = None
        usage = data.get("usage") if isinstance(data, dict) else None
        if isinstance(usage, dict) and usage.get("cost") is not None:
            try:
                reported = float(usage["cost"])
            except (TypeError, ValueError):
                reported = None
        return data, reported

    def classify(self, model: str, state: dict) -> ClassifierResult:
        data, reported = self._post(model, state, JEV_QUESTIONS)
        try:
            result = parse_answers(data)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise OutputInvalid(f"unexpected answers: {e!r}", reported_cost=reported) from e
        return replace(result, reported_cost=reported)

    def choose(self, model: str, state: dict, question: dict) -> ChoiceResult:
        data, reported = self._post(model, state, {"choice": question})
        try:
            result = parse_choice(data, question)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise OutputInvalid(f"unexpected choice: {e!r}", reported_cost=reported) from e
        return replace(result, reported_cost=reported)
```

Update its import: `from pulse.agents.classifier import JEV_QUESTIONS, ChoiceResult, ClassifierResult, parse_answers, parse_choice`.

`pulse/agents/llm.py`:
- import `ChoiceResult` alongside the existing classifier imports;
- add after `ClassifyResponse`:

```python
@dataclass(frozen=True)
class ChoiceResponse:
    result: ChoiceResult
    run_id: int
```

- replace `classify` with `_classifier_call`, `classify` and `choose`:

```python
    def _classifier_call(self, call: Callable[[Classifier, str], Any]) -> tuple[Any, int]:
        """Budget gate, transient backoff, one retry on invalid output, one agent_runs row."""
        cfg = self._config.classifier
        if cfg is None or self._classifier is None:
            raise RuntimeError("classifier not configured")
        ref = cfg.model
        price = self._config.pricing.get(str(ref))
        started = self._now()
        if self.spent_today() >= self._config.daily_usd_cap:
            self._record("classifier", ref, started, "skipped_budget", "daily budget cap reached", _Usage())
            raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

        usage = _Usage()
        error = "no attempt made"
        for _ in range(MAX_VALIDATION_ATTEMPTS):
            try:
                result = self._with_backoff(lambda: call(self._classifier, ref.model))
            except OutputInvalid as e:
                usage.cost_usd += request_cost(price, e.reported_cost)
                error = f"invalid output: {e}"
                continue
            except (TransientError, ProviderError) as e:
                error = f"provider error: {e}"
                break
            usage.cost_usd += request_cost(price, result.reported_cost)
            return result, self._record("classifier", ref, started, "ok", None, usage)

        self._record("classifier", ref, started, "failed", error, usage)
        raise LLMError(f"classifier: {error}")

    def classify(self, state: dict) -> ClassifyResponse:
        """Ask the classifier (Jev) the triage questions about one message state."""
        result, run_id = self._classifier_call(lambda c, model: c.classify(model, state))
        return ClassifyResponse(result, run_id)

    def choose(self, state: dict, question: dict) -> ChoiceResponse:
        """Ask the classifier (Jev) one choice question about one message state."""
        result, run_id = self._classifier_call(lambda c, model: c.choose(model, state, question))
        return ChoiceResponse(result, run_id)
```

`tests/fakes.py` — replace `FakeClassifier` with:

```python
class FakeClassifier:
    """Like FakeBackend: queued items first, then a handler. Exceptions are raised.

    classify: `responses` then `handler(state)`; choose: `choices` then `choose_handler(state, question)`.
    """

    def __init__(self, responses=None, handler=None, choices=None, choose_handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.choices = list(choices or [])
        self.choose_handler = choose_handler
        self.calls: list[dict] = []
        self.choose_calls: list[dict] = []

    def classify(self, model, state):
        self.calls.append({"model": model, "state": state})
        item = self.responses.pop(0) if self.responses else self.handler
        if item is None:
            raise AssertionError("FakeClassifier has no response queued")
        if callable(item) and not isinstance(item, ClassifierResult):
            item = item(state)
        if isinstance(item, Exception):
            raise item
        return item

    def choose(self, model, state, question):
        self.choose_calls.append({"model": model, "state": state, "question": question})
        item = self.choices.pop(0) if self.choices else self.choose_handler
        if item is None:
            raise AssertionError("FakeClassifier has no choice queued")
        if callable(item) and not isinstance(item, ChoiceResult):
            item = item(state, question)
        if isinstance(item, Exception):
            raise item
        return item
```

and add `ChoiceResult` to the `pulse.agents.classifier` import in `tests/fakes.py`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_jev_backend.py tests/test_llm_classify.py tests/test_triage_classifier.py -v` — Expected: all pass (existing classify tests unchanged).

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/agents/classifier.py pulse/agents/providers/jev_backend.py pulse/agents/llm.py tests/fakes.py tests/test_jev_backend.py tests/test_llm_classify.py
git commit -m "feat: Jev theme choice and LLMClient.choose" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Theme agent

**Files:**
- Create: `pulse/agents/theme.py`, `pulse/agents/prompts/theme_v1.md`
- Test: `tests/test_theme_agent.py`

**Interfaces:**
- Consumes: `LLMClient.complete`, `.choose`, `.config`, `.has_classifier`, `.db_lock` (Tasks 4, Plan 2); `theme_question` (Task 4); `active_themes`, `apply_proposal`, `assign`, `mark_themed`, `ThemeBudget` (Task 3).
- Produces:
  - `THEME_SCHEMA`, `SCHEMA_NAME = "theme_result"`, `BATCH_SIZE = 60`, `MAX_CANDIDATES = 600`, `MAX_THEMES_FOR_JEV = 40`
  - `@dataclass ThemeStats(considered=0, jev_assigned=0, llm_batches=0, failed_batches=0, skipped_budget=False, created=0, assigned=0, merged=0, renamed=0, rejected=list)`
  - `select_candidates(conn, limit=MAX_CANDIDATES) -> list[sqlite3.Row]` (non-team messages with non-empty topics and `themed_at IS NULL`, newest first)
  - `validate_proposal(data: dict, message_ids: set[str], theme_ids: set[int]) -> None` (raises `ValueError` on any id outside the batch or the active themes)
  - `run_themes(conn, llm, now, *, concurrency=8) -> ThemeStats`

Flow: Stage A (only when the classifier is enabled, configured, and at least one theme exists) asks Jev per candidate over at most 40 active themes; a choice other than `none` with confidence ≥ `min_confidence` is assigned and marked themed; everything else goes to Stage B. A budget stop in Stage A ends the run. Stage B sends sequential batches of 60 to the theme LLM with the current active themes; a valid proposal is applied under the run's `ThemeBudget` and the batch's messages are marked themed (whether or not they got a theme); a failed batch leaves its messages unthemed for the next run; a budget stop ends the run.

- [ ] **Step 1: Write the prompt** `pulse/agents/prompts/theme_v1.md`:

```markdown
You maintain the list of recurring themes (pain points and wins) in a developer product's Discord community, so the community team can see what keeps coming up.

You receive JSON with:
- themes: the current active themes, each with id, name and description.
- messages: recent messages not yet assigned to a theme, each with message_id, content, kind, sentiment (-2..2) and topics.

Return:
- assignments: for messages that clearly fit existing themes, the message_id and the theme_ids (usually one) they belong to.
- new_themes: only for a recurring problem or win that no existing theme covers. Give a short name (2-5 words, like "Auth docs gaps" or "M1 install failures"), a one-sentence description, and the message_ids that belong to it. Prefer fewer, broader themes; do not create a theme for a single one-off message unless it is severe.
- merges: when two existing themes are clearly the same thing, merge the narrower (from_id) into the broader (into_id), with a short reason. Be conservative.
- renames: only when a theme's name no longer describes what it collects.

Rules:
- Use only theme ids from the input themes and message ids from the input messages.
- A message may be left unassigned if it fits nothing and is not worth a new theme (chit-chat, one-off questions).
- Name themes by product area and problem, not by emotion ("Rate limit errors", not "Angry users").
- Return empty lists when there is nothing to do.
```

- [ ] **Step 2: Write the failing tests** — `tests/test_theme_agent.py`:

```python
import json

from pulse.agents.base import BackendResult
from pulse.agents.classifier import ChoiceResult
from pulse.agents.theme import run_themes, select_candidates
from pulse.db import connect
from pulse.store import upsert_messages
from pulse.themes import create_theme
from tests.fakes import (
    T0, FakeBackend, FakeClassifier, classifier_config, make_config, make_llm, msg, set_triage,
)

NOW = T0


def seed(*specs):
    """specs: (id, topics, author_id)"""
    conn = connect(":memory:")
    upsert_messages(conn, [msg(i, f"text {i}", minutes=n, author_id=a) for n, (i, _, a) in enumerate(specs)],
                    frozenset({"t1"}))
    for i, topics, _ in specs:
        set_triage(conn, i, sentiment=-1, kind="bug", topics=topics)
    return conn


def proposal(**overrides):
    base = {"assignments": [], "new_themes": [], "merges": [], "renames": []}
    base.update(overrides)
    return BackendResult(base, 100, 20)


def all_messages_into_new_theme(user):
    ids = [m["message_id"] for m in json.loads(user)["messages"]]
    return proposal(new_themes=[{"name": "Install failures", "description": "wheels", "message_ids": ids}])


def themed(conn):
    return {r[0] for r in conn.execute("SELECT message_id FROM triage WHERE themed_at IS NOT NULL")}


def links(conn):
    return dict(conn.execute("SELECT message_id, theme_id FROM message_themes").fetchall())


def test_candidates_need_topics_and_exclude_staff():
    conn = seed(("a", ["install"], "u1"), ("b", [], "u1"), ("s", ["install"], "t1"))
    assert [r["id"] for r in select_candidates(conn)] == ["a"]


def test_llm_creates_first_theme_and_marks_batch_themed():
    conn = seed(("a", ["install"], "u1"), ("b", ["install"], "u2"))
    backend = FakeBackend(handler=all_messages_into_new_theme)
    stats = run_themes(conn, make_llm(conn, make_config(), backend), NOW)
    assert (stats.considered, stats.llm_batches, stats.created, stats.assigned) == (2, 1, 1, 2)
    assert set(links(conn)) == {"a", "b"}
    assert themed(conn) == {"a", "b"}
    assert backend.calls[0]["schema_name"] == "theme_result"
    assert json.loads(backend.calls[0]["user"])["themes"] == []


def test_jev_assigns_confident_matches_and_llm_gets_leftovers():
    conn = seed(("a", ["install"], "u1"), ("b", ["auth"], "u2"))
    with conn:
        tid, _ = create_theme(conn, "Install failures", "wheels", NOW)

    def pick(state, question):
        return ChoiceResult(str(tid), 0.95) if state["message_id"] == "a" else ChoiceResult("none", 0.9)

    fc = FakeClassifier(choose_handler=pick)
    backend = FakeBackend(handler=lambda user: proposal())
    config = make_config(classifier=classifier_config())
    stats = run_themes(conn, make_llm(conn, config, backend, classifier=fc), NOW)
    assert stats.jev_assigned == 1
    assert links(conn) == {"a": tid}
    assert [m["message_id"] for m in json.loads(backend.calls[0]["user"])["messages"]] == ["b"]
    assert themed(conn) == {"a", "b"}


def test_low_confidence_jev_choice_goes_to_llm():
    conn = seed(("a", ["install"], "u1"))
    with conn:
        tid, _ = create_theme(conn, "Install failures", "wheels", NOW)
    fc = FakeClassifier(choices=[ChoiceResult(str(tid), 0.3)])
    backend = FakeBackend(handler=lambda user: proposal(assignments=[{"message_id": "a", "theme_ids": [tid]}]))
    stats = run_themes(conn, make_llm(conn, make_config(classifier=classifier_config()), backend, classifier=fc), NOW)
    assert (stats.jev_assigned, stats.llm_batches, stats.assigned) == (0, 1, 1)


def test_invalid_proposal_is_retried_then_left_unthemed():
    conn = seed(("a", ["install"], "u1"))
    bad = proposal(assignments=[{"message_id": "a", "theme_ids": [999]}])
    backend = FakeBackend(responses=[bad, bad])
    stats = run_themes(conn, make_llm(conn, make_config(), backend), NOW)
    assert (stats.failed_batches, stats.assigned) == (1, 0)
    assert themed(conn) == set()
    assert len(backend.calls) == 2


def test_budget_stop_leaves_messages_for_next_run():
    conn = seed(("a", ["install"], "u1"))
    backend = FakeBackend(handler=all_messages_into_new_theme)
    stats = run_themes(conn, make_llm(conn, make_config(daily_usd_cap=0.0), backend), NOW)
    assert stats.skipped_budget is True
    assert backend.calls == [] and themed(conn) == set()


def test_second_run_is_a_no_op():
    conn = seed(("a", ["install"], "u1"))
    backend = FakeBackend(handler=all_messages_into_new_theme)
    llm = make_llm(conn, make_config(), backend)
    run_themes(conn, llm, NOW)
    stats = run_themes(conn, llm, NOW)
    assert stats.considered == 0 and len(backend.calls) == 1
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_theme_agent.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.agents.theme'`

- [ ] **Step 4: Implement** `pulse/agents/theme.py`:

```python
"""Theme agent: group labelled messages into recurring themes (pain points and wins).

Stage A: Jev picks an existing theme per message when it is confident.
Stage B: the theme LLM proposes assignments, new themes, merges and renames for
the rest, one batch at a time so each batch sees the themes the last one made.
Plain code applies proposals with guardrails (pulse.themes).
"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pulse.agents.base import BudgetExceeded, LLMError
from pulse.agents.classifier import theme_question
from pulse.agents.llm import LLMClient
from pulse.themes import ThemeBudget, active_themes, apply_proposal, assign, mark_themed

SCHEMA_NAME = "theme_result"
BATCH_SIZE = 60
MAX_CANDIDATES = 600
MAX_THEMES_FOR_JEV = 40
MAX_CONTENT_CHARS = 500

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_INT_LIST = {"type": "array", "items": {"type": "integer"}}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


THEME_SCHEMA = _obj({
    "assignments": {"type": "array", "items": _obj({"message_id": {"type": "string"}, "theme_ids": _INT_LIST})},
    "new_themes": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "description": {"type": "string"}, "message_ids": _STR_LIST,
    })},
    "merges": {"type": "array", "items": _obj({
        "from_id": {"type": "integer"}, "into_id": {"type": "integer"}, "reason": {"type": "string"},
    })},
    "renames": {"type": "array", "items": _obj({
        "theme_id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"},
    })},
})


@dataclass
class ThemeStats:
    considered: int = 0
    jev_assigned: int = 0
    llm_batches: int = 0
    failed_batches: int = 0
    skipped_budget: bool = False
    created: int = 0
    assigned: int = 0
    merged: int = 0
    renamed: int = 0
    rejected: list[str] = field(default_factory=list)


def load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "theme_v1.md").read_text(encoding="utf-8")


def select_candidates(conn: sqlite3.Connection, limit: int = MAX_CANDIDATES) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id, m.content, t.kind, t.sentiment, t.topics FROM messages m"
        " JOIN triage t ON t.message_id = m.id"
        " WHERE t.themed_at IS NULL AND t.topics != '[]' AND m.is_team = 0 AND m.is_bot = 0"
        " ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def _state(row: sqlite3.Row) -> dict:
    content = row["content"]
    return {
        "message_id": row["id"],
        "content": content if len(content) <= MAX_CONTENT_CHARS else content[:MAX_CONTENT_CHARS] + "…",
        "kind": row["kind"],
        "sentiment": row["sentiment"],
        "topics": json.loads(row["topics"]),
    }


def validate_proposal(data: dict, message_ids: set[str], theme_ids: set[int]) -> None:
    bad_messages = sorted(
        {a["message_id"] for a in data["assignments"]} | {m for t in data["new_themes"] for m in t["message_ids"]}
        - message_ids
    )
    bad_themes = sorted(
        ({tid for a in data["assignments"] for tid in a["theme_ids"]}
         | {m["from_id"] for m in data["merges"]} | {m["into_id"] for m in data["merges"]}
         | {r["theme_id"] for r in data["renames"]})
        - theme_ids
    )
    if bad_messages or bad_themes:
        raise ValueError(f"unknown message ids {bad_messages}, unknown theme ids {bad_themes}")


def _jev_stage(conn, llm, rows, themes, concurrency, stats, now) -> list[sqlite3.Row]:
    cfg = llm.config.classifier
    question = theme_question(themes[:MAX_THEMES_FOR_JEV])
    states = [_state(r) for r in rows]
    by_id = {r["id"]: r for r in rows}

    def call(state):
        try:
            return state["message_id"], llm.choose(state, question), None
        except BudgetExceeded:
            return state["message_id"], None, "budget"
        except LLMError:
            return state["message_id"], None, "failed"

    leftovers = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for fut in as_completed([pool.submit(call, s) for s in states]):
            mid, resp, error = fut.result()
            if error == "budget":
                stats.skipped_budget = True
                continue
            if error is None and resp.result.choice != "none" and resp.result.confidence >= cfg.min_confidence:
                with llm.db_lock, conn:
                    assign(conn, mid, int(resp.result.choice))
                    mark_themed(conn, [mid], now)
                stats.jev_assigned += 1
                continue
            leftovers.append(by_id[mid])
    leftovers.sort(key=lambda r: r["id"])
    return leftovers


def run_themes(conn: sqlite3.Connection, llm: LLMClient, now: datetime, *, concurrency: int = 8) -> ThemeStats:
    stats = ThemeStats()
    rows = select_candidates(conn)
    stats.considered = len(rows)
    if not rows:
        return stats

    themes = active_themes(conn)
    cfg = llm.config.classifier
    if themes and cfg is not None and cfg.enabled and llm.has_classifier:
        rows = _jev_stage(conn, llm, rows, themes, concurrency, stats, now)
        if stats.skipped_budget:
            return stats

    budget = ThemeBudget()
    system = load_prompt()
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        ids = {r["id"] for r in batch}
        current = active_themes(conn)
        theme_ids = {t["id"] for t in current}
        user = json.dumps({
            "themes": [{"id": t["id"], "name": t["name"], "description": t["description"]} for t in current],
            "messages": [_state(r) for r in batch],
        }, ensure_ascii=False)
        try:
            resp = llm.complete(
                "theme", system, user, THEME_SCHEMA, SCHEMA_NAME,
                validate=lambda data: validate_proposal(data, ids, theme_ids),
            )
        except BudgetExceeded:
            stats.skipped_budget = True
            break
        except LLMError:
            stats.failed_batches += 1
            continue
        stats.llm_batches += 1
        with llm.db_lock, conn:
            changes = apply_proposal(conn, resp.data, now, resp.run_id, budget)
            mark_themed(conn, ids, now)
        stats.created += changes.created
        stats.assigned += changes.assigned
        stats.merged += changes.merged
        stats.renamed += changes.renamed
        stats.rejected += changes.rejected
    return stats
```


- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_theme_agent.py -v` — Expected: 7 passed.

- [ ] **Step 6: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/agents/theme.py pulse/agents/prompts/theme_v1.md tests/test_theme_agent.py
git commit -m "feat: theme agent with Jev assignment and guarded LLM proposals" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Digest agent (weekly and launch)

**Files:**
- Create: `pulse/agents/digest.py`, `pulse/agents/prompts/digest_v1.md`
- Test: `tests/test_digest.py`

**Interfaces:**
- Consumes: `stats` (Task 2), `strip_unknown`, `cited_ids` (Task 1), `sync_launches` (Task 1), `LLMClient.complete`.
- Produces:
  - `DIGEST_SCHEMA` (`{"markdown": string}`), `SCHEMA_NAME = "digest_result"`, `MAX_MESSAGES = 60`, `LAUNCH_WINDOW_DAYS = 14`
  - `@dataclass(frozen=True) DigestResult(digest_id: int, kind: str, period_start: str, period_end: str, markdown: str, cited_message_ids: list[str], removed_citations: list[str])`
  - `build_weekly_input(conn, now) -> dict` keys `kind, period, current, previous, top_themes, mod_queue, messages`
  - `build_launch_input(conn, launch_row, now) -> dict` keys `kind, launch, before, after, top_themes_after, messages`
  - `run_digest(conn, llm, now, *, launch: str | None = None) -> DigestResult` (raises `LookupError` for an unknown launch; `BudgetExceeded`/`LLMError` propagate; saves a `digests` row)

Samples (deduplicated by id, capped at 60): weekly = up to 4 most negative messages for each of the top 5 themes, then 8 praise messages (most positive first), then 10 most negative messages overall; launch = up to 30 messages after the launch matching any keyword (case-insensitive substring, most negative first), then 8 praise and 10 most negative messages after the launch. Launch windows: before = `[date − 14d, date)`, after = `[date, min(date + 14d, now))` (empty when the launch is in the future).

- [ ] **Step 1: Write the prompt** `pulse/agents/prompts/digest_v1.md`:

```markdown
You write the community digest for a developer product's DevRel team: what is landing well, what hurts, and what to do first. The reader has a few minutes.

You receive JSON with precomputed statistics and a sample of real messages (each with message_id, author, channel, created_at, kind, sentiment -2..2, content). A weekly digest compares this week to last week; a launch digest compares the 14 days before a launch with the days after it and includes messages mentioning the launch keywords.

Write markdown with exactly these sections, in this order:

## What's landing well
## Top pain points
## Needs attention
## Suggested priorities

Rules:
- Lead each section with its point in one sentence, using numbers from the stats (counts, changes vs the previous period, pain point scores).
- Back every claim about users with a citation to a message from the input, written exactly as [[msg:<message_id>]]. Cite only message_ids that appear in the input. Never invent quotes; you may quote short phrases from a cited message's content.
- Top pain points: one bullet per theme, most important first, with its trend and 1-2 citations.
- Needs attention: the open mod queue count and the most urgent unanswered or frustrated messages.
- Suggested priorities: 3-5 concrete actions (fix, doc change, reply), each tied to a pain point.
- Plain, specific language. No preamble, no sign-off. Under 400 words.
```

- [ ] **Step 2: Write the failing tests** — `tests/test_digest.py`:

```python
import json
from datetime import timedelta

import pytest

from pulse.agents.base import BackendResult
from pulse.agents.digest import run_digest
from pulse.config import Launch
from pulse.db import connect
from pulse.store import sync_launches, upsert_messages
from pulse.themes import assign, create_theme
from tests.fakes import T0, FakeBackend, make_config, make_llm, msg, set_triage

NOW = T0 + timedelta(days=1)


def seed():
    conn = connect(":memory:")
    upsert_messages(conn, [
        msg("bug1", "v2 install fails on M1", minutes=0),
        msg("bug2", "still broken after the v2 upgrade", minutes=10),
        msg("love", "v2 builds are so fast", minutes=20),
        msg("old", "v1 was fine", minutes=-9 * 24 * 60),
    ], frozenset())
    set_triage(conn, "bug1", sentiment=-2, kind="bug", topics=["m1 install"])
    set_triage(conn, "bug2", sentiment=-1, kind="bug", topics=["m1 install"])
    set_triage(conn, "love", sentiment=2, kind="praise", topics=["build speed"])
    set_triage(conn, "old", sentiment=0, kind="other")
    with conn:
        tid, _ = create_theme(conn, "M1 install failures", "arm64 wheels", T0)
        assign(conn, "bug1", tid)
        assign(conn, "bug2", tid)
    return conn


def cite_first_and_bogus(user):
    first = json.loads(user)["messages"][0]["message_id"]
    return BackendResult({"markdown": f"## What's landing well\nFast builds [[msg:{first}]] [[msg:bogus]]."}, 500, 200)


def test_weekly_digest_input_has_stats_themes_and_samples():
    conn = seed()
    backend = FakeBackend(handler=cite_first_and_bogus)
    run_digest(conn, make_llm(conn, make_config(), backend), NOW)
    data = json.loads(backend.calls[0]["user"])
    assert data["kind"] == "weekly"
    assert data["current"]["messages"] == 3
    assert data["top_themes"][0]["name"] == "M1 install failures"
    ids = [m["message_id"] for m in data["messages"]]
    assert {"bug1", "bug2", "love"} <= set(ids) and "old" not in ids
    assert len(ids) == len(set(ids))
    assert backend.calls[0]["schema_name"] == "digest_result"


def test_weekly_digest_strips_unknown_citations():
    conn = seed()
    result = run_digest(conn, make_llm(conn, make_config(), FakeBackend(handler=cite_first_and_bogus)), NOW)
    assert result.removed_citations == ["bogus"]
    assert "[[msg:bogus]]" not in result.markdown
    row = conn.execute("SELECT * FROM digests WHERE id = ?", (result.digest_id,)).fetchone()
    assert row["kind"] == "weekly"
    assert json.loads(row["cited_message_ids"]) == result.cited_message_ids
    assert len(result.cited_message_ids) == 1


def test_launch_digest_uses_before_after_windows_and_keywords():
    conn = seed()
    sync_launches(conn, [Launch("v2.0 SDK", "2026-09-28", ("v2",))])
    backend = FakeBackend(handler=cite_first_and_bogus)
    result = run_digest(conn, make_llm(conn, make_config(), backend), NOW, launch="v2.0 SDK")
    data = json.loads(backend.calls[0]["user"])
    assert data["kind"] == "launch"
    assert data["launch"] == {"name": "v2.0 SDK", "date": "2026-09-28", "keywords": ["v2"]}
    assert data["after"]["messages"] == 3
    assert data["before"]["messages"] == 1
    assert [m["message_id"] for m in data["messages"]][:2] == ["bug1", "bug2"]
    assert result.kind == "launch"
    assert conn.execute("SELECT launch_id FROM digests").fetchone()[0] is not None


def test_unknown_launch_raises_lookup_error():
    conn = seed()
    with pytest.raises(LookupError, match="nope"):
        run_digest(conn, make_llm(conn, make_config(), FakeBackend()), NOW, launch="nope")


def test_blank_markdown_is_rejected_and_retried():
    conn = seed()
    backend = FakeBackend(responses=[BackendResult({"markdown": "  "}, 1, 1)], handler=cite_first_and_bogus)
    result = run_digest(conn, make_llm(conn, make_config(), backend), NOW)
    assert len(backend.calls) == 2 and result.markdown.strip()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_digest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.agents.digest'`

- [ ] **Step 4: Implement** `pulse/agents/digest.py`:

```python
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

SCHEMA_NAME = "digest_result"
MAX_MESSAGES = 60
LAUNCH_WINDOW_DAYS = 14
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


def build_weekly_input(conn: sqlite3.Connection, now: datetime) -> dict:
    start = now - timedelta(days=7)
    previous_start = start - timedelta(days=7)
    top = stats.theme_scores(conn, now, window_days=7, limit=10)
    messages: list[dict] = []
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
    clause = " OR ".join("lower(m.content) LIKE ?" for _ in keywords)
    rows = conn.execute(
        f"SELECT {stats.MESSAGE_COLUMNS} FROM messages m JOIN triage t ON t.message_id = m.id"
        " WHERE m.is_team = 0 AND m.is_bot = 0 AND m.created_at >= ? AND m.created_at < ?"
        f" AND ({clause}) ORDER BY t.sentiment ASC, m.created_at DESC, m.id LIMIT ?",
        [to_iso(start), to_iso(end), *[f"%{k.lower()}%" for k in keywords], limit],
    ).fetchall()
    return [stats.to_message(r) for r in rows]


def build_launch_input(conn: sqlite3.Connection, launch: sqlite3.Row, now: datetime) -> dict:
    day = datetime.combine(date.fromisoformat(launch["date"]), time.min, timezone.utc)
    before_start = day - timedelta(days=LAUNCH_WINDOW_DAYS)
    after_end = max(day, min(day + timedelta(days=LAUNCH_WINDOW_DAYS), now))
    keywords = json.loads(launch["keywords"])
    messages = _keyword_messages(conn, day, after_end, keywords)
    if after_end > day:
        messages += stats.sample_messages(conn, day, after_end, kinds=("praise",), limit=8, most_negative=False)
        messages += stats.sample_messages(conn, day, after_end, limit=10)
    return {
        "kind": "launch",
        "launch": {"name": launch["name"], "date": launch["date"], "keywords": keywords},
        "before": stats.period_summary(conn, before_start, day),
        "after": stats.period_summary(conn, day, after_end),
        "top_themes_after": [
            asdict(s) for s in stats.theme_scores(conn, after_end, window_days=LAUNCH_WINDOW_DAYS, limit=10)
        ],
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
    else:
        row = conn.execute("SELECT * FROM launches WHERE name = ?", (launch,)).fetchone()
        if row is None:
            raise LookupError(f"unknown launch {launch!r}; add it under [[launches]] in pulse.toml")
        launch_id = row["id"]
        data = build_launch_input(conn, row, now)
        period = (data["before"]["start"], data["after"]["end"])

    allowed = {m["message_id"] for m in data["messages"]}
    resp = llm.complete(
        "digest", load_prompt(), json.dumps(data, ensure_ascii=False), DIGEST_SCHEMA, SCHEMA_NAME,
        validate=_require_text,
    )
    markdown, removed = strip_unknown(resp.data["markdown"], allowed)
    cited = cited_ids(markdown)
    with conn:
        digest_id = int(conn.execute(
            "INSERT INTO digests (kind, period_start, period_end, launch_id, markdown, cited_message_ids,"
            " run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (data["kind"], period[0], period[1], launch_id, markdown, json.dumps(cited), resp.run_id, to_iso(now)),
        ).lastrowid)
    return DigestResult(digest_id, data["kind"], period[0], period[1], markdown, cited, removed)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_digest.py -v` — Expected: 5 passed.

- [ ] **Step 6: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/agents/digest.py pulse/agents/prompts/digest_v1.md tests/test_digest.py
git commit -m "feat: weekly and launch digest agent with verified citations" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Tool-calling contract and `LLMClient.run_tools`

**Files:**
- Modify: `pulse/agents/base.py`, `pulse/agents/llm.py`, `tests/fakes.py`
- Test: `tests/test_llm_tools.py`

**Interfaces:**
- Produces:
  - `base.py`: `@dataclass(frozen=True) ToolSpec(name: str, description: str, parameters: dict)`; `@dataclass(frozen=True) ToolCall(id: str, name: str, arguments: dict)`; `@dataclass(frozen=True) StepResult(text: str | None, tool_calls: tuple[ToolCall, ...], input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, reported_cost: float | None = None)`; `class ToolBackend(Protocol): tool_step(self, model, system, transcript: list[dict], tools: list[ToolSpec], allow_tools: bool = True) -> StepResult`; `class ToolError(Exception)` (a tool rejected its arguments; the message goes back to the model).
  - Neutral transcript turns: `{"role": "user", "content": str}`, `{"role": "assistant", "text": str | None, "tool_calls": list[ToolCall]}`, `{"role": "tool", "results": [{"id": str, "content": str}], "note": str (optional)}`.
  - `llm.py`: `@dataclass(frozen=True) ToolRunResponse(text: str, run_id: int, tool_calls: int)`; `LLMClient.run_tools(agent, system, user, tools, execute: Callable[[ToolCall], str], *, max_calls: int = 12) -> ToolRunResponse`. One `agent_runs` row per run. Budget gate at the start and before each tool round (counting this run's cost so far). Transient errors back off per step; provider errors and invalid output fail the run (`LLMError`). After `max_calls` tool calls the next step has `allow_tools=False` and the last tool turn carries a note asking for the final answer. An empty final answer fails the run.
  - `tests/fakes.py`: `FakeBackend(responses=None, handler=None, steps=None, step_handler=None)` with `.step_calls` (each: `model, system, transcript` (deep copy), `tools` (names), `allow_tools`); `step_handler(transcript, allow_tools)`.

- [ ] **Step 1: Write the failing tests** — `tests/test_llm_tools.py`:

```python
import pytest

from pulse.agents.base import (
    BudgetExceeded, LLMError, ProviderError, StepResult, ToolCall, ToolError, ToolSpec, TransientError,
)
from pulse.db import connect
from tests.fakes import FakeBackend, make_config, make_llm

TOOLS = [ToolSpec("lookup", "Look something up.", {"type": "object", "properties": {"q": {"type": "string"}}})]


def step(text=None, *calls, inp=100, out=20):
    return StepResult(text, tuple(calls), inp, out)


def run(backend, execute=lambda call: f"result for {call.arguments.get('q')}", **config_overrides):
    conn = connect(":memory:")
    sleeps = []
    llm = make_llm(conn, make_config(**config_overrides), backend, sleeps=sleeps)
    return conn, llm, sleeps


def runs(conn):
    return conn.execute("SELECT * FROM agent_runs").fetchall()


def test_tool_round_then_final_answer():
    backend = FakeBackend(steps=[
        step("checking", ToolCall("c1", "lookup", {"q": "auth"})),
        step("Auth docs are the problem."),
    ])
    conn, llm, _ = run(backend)
    executed = []
    resp = llm.run_tools("investigate", "sys", "why?", TOOLS, lambda c: executed.append(c) or "3 hits")
    assert (resp.text, resp.tool_calls) == ("Auth docs are the problem.", 1)
    assert executed == [ToolCall("c1", "lookup", {"q": "auth"})]
    second = backend.step_calls[1]["transcript"]
    assert second[1] == {"role": "assistant", "text": "checking", "tool_calls": [ToolCall("c1", "lookup", {"q": "auth"})]}
    assert second[2] == {"role": "tool", "results": [{"id": "c1", "content": "3 hits"}]}
    [row] = runs(conn)
    assert (row["agent"], row["status"], row["input_tokens"], row["output_tokens"]) == ("investigate", "ok", 200, 40)
    assert resp.run_id == row["id"]


def test_tool_errors_go_back_to_the_model():
    def boom(call):
        raise ToolError("bad date")

    backend = FakeBackend(steps=[step(None, ToolCall("c1", "lookup", {})), step("done")])
    conn, llm, _ = run(backend)
    llm.run_tools("investigate", "sys", "q", TOOLS, boom)
    assert backend.step_calls[1]["transcript"][2]["results"] == [{"id": "c1", "content": "error: bad date"}]


def test_tool_call_limit_forces_a_final_answer():
    backend = FakeBackend(steps=[
        step(None, ToolCall("c1", "lookup", {}), ToolCall("c2", "lookup", {})),
        step("final"),
    ])
    conn, llm, _ = run(backend)
    resp = llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "x", max_calls=2)
    assert resp.text == "final"
    assert [c["allow_tools"] for c in backend.step_calls] == [True, False]
    assert "final answer" in backend.step_calls[1]["transcript"][2]["note"]


def test_transient_errors_back_off_per_step():
    backend = FakeBackend(steps=[TransientError("429"), step("ok")])
    conn, llm, sleeps = run(backend)
    assert llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "").text == "ok"
    assert sleeps == [1]


def test_provider_error_fails_the_run():
    backend = FakeBackend(steps=[ProviderError("401")])
    conn, llm, _ = run(backend)
    with pytest.raises(LLMError, match="investigate"):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "")
    assert runs(conn)[0]["status"] == "failed"


def test_empty_final_answer_fails():
    backend = FakeBackend(steps=[step("   ")])
    conn, llm, _ = run(backend)
    with pytest.raises(LLMError, match="empty"):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "")


def test_budget_cap_blocks_the_run():
    backend = FakeBackend(steps=[step("never")])
    conn, llm, _ = run(backend, daily_usd_cap=0.0)
    with pytest.raises(BudgetExceeded):
        llm.run_tools("investigate", "sys", "q", TOOLS, lambda c: "")
    assert backend.step_calls == []
    assert runs(conn)[0]["status"] == "skipped_budget"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_llm_tools.py -v`
Expected: FAIL with `ImportError: cannot import name 'StepResult'`

- [ ] **Step 3: Implement**

Append to `pulse/agents/base.py`:

```python
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema for the arguments


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass(frozen=True)
class StepResult:
    text: str | None
    tool_calls: tuple[ToolCall, ...]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    reported_cost: float | None = None


class ToolBackend(Protocol):
    """One model turn in a tool loop.

    transcript turns: {"role": "user", "content": str}
                      {"role": "assistant", "text": str | None, "tool_calls": list[ToolCall]}
                      {"role": "tool", "results": [{"id": str, "content": str}], "note": str (optional)}
    """

    def tool_step(
        self, model: str, system: str, transcript: list[dict], tools: list[ToolSpec], allow_tools: bool = True
    ) -> StepResult: ...


class ToolError(Exception):
    """A tool rejected its arguments. The message is returned to the model, not raised to the caller."""
```

In `pulse/agents/llm.py`: import `StepResult`, `ToolCall`, `ToolError`, `ToolSpec` from `pulse.agents.base`; add `NoReturn` to the typing import; add after `ChoiceResponse`:

```python
@dataclass(frozen=True)
class ToolRunResponse:
    text: str
    run_id: int
    tool_calls: int


TOOL_LIMIT_NOTE = "Tool call limit reached. Write your final answer now from what you have."
```

and add these methods to `LLMClient`:

```python
    def run_tools(
        self,
        agent: str,
        system: str,
        user: str,
        tools: list[ToolSpec],
        execute: Callable[[ToolCall], str],
        *,
        max_calls: int = 12,
    ) -> ToolRunResponse:
        """Run a tool loop until the model answers without tools. One agent_runs row per run."""
        ref = self._config.models[agent]
        price = self._config.pricing.get(str(ref))
        started = self._now()
        if self.spent_today() >= self._config.daily_usd_cap:
            self._record(agent, ref, started, "skipped_budget", "daily budget cap reached", _Usage())
            raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

        backend = self._backends[ref.provider]
        transcript: list[dict] = [{"role": "user", "content": user}]
        usage = _Usage()
        calls = 0
        while True:
            allow = calls < max_calls
            try:
                step: StepResult = self._with_backoff(
                    lambda: backend.tool_step(ref.model, system, transcript, tools, allow)
                )
            except OutputInvalid as e:
                usage.add(price, e.input_tokens, e.output_tokens, e.cache_read_tokens, e.reported_cost)
                self._fail_run(agent, ref, started, usage, f"invalid output: {e}")
            except (TransientError, ProviderError) as e:
                self._fail_run(agent, ref, started, usage, f"provider error: {e}")
            usage.add(price, step.input_tokens, step.output_tokens, step.cache_read_tokens, step.reported_cost)

            if not step.tool_calls or not allow:
                text = (step.text or "").strip()
                if not text:
                    self._fail_run(agent, ref, started, usage, "empty final answer")
                return ToolRunResponse(text, self._record(agent, ref, started, "ok", None, usage), calls)

            if self.spent_today() + usage.cost_usd >= self._config.daily_usd_cap:
                self._record(agent, ref, started, "failed", "daily budget cap reached mid-run", usage)
                raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

            transcript.append({"role": "assistant", "text": step.text, "tool_calls": list(step.tool_calls)})
            results = []
            for call in step.tool_calls:
                calls += 1
                try:
                    content = execute(call)
                except ToolError as e:
                    content = f"error: {e}"
                results.append({"id": call.id, "content": content})
            turn: dict = {"role": "tool", "results": results}
            if calls >= max_calls:
                turn["note"] = TOOL_LIMIT_NOTE
            transcript.append(turn)

    def _fail_run(self, agent: str, ref: ModelRef, started: datetime, usage: _Usage, error: str) -> NoReturn:
        self._record(agent, ref, started, "failed", error, usage)
        raise LLMError(f"{agent}: {error}")
```

In `tests/fakes.py`, add `import copy` at the top, import `StepResult` from `pulse.agents.base`, and extend `FakeBackend`:

```python
    def __init__(self, responses=None, handler=None, steps=None, step_handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.steps = list(steps or [])
        self.step_handler = step_handler
        self.calls: list[dict] = []
        self.step_calls: list[dict] = []

    def tool_step(self, model, system, transcript, tools, allow_tools=True):
        self.step_calls.append({
            "model": model, "system": system, "transcript": copy.deepcopy(transcript),
            "tools": [t.name for t in tools], "allow_tools": allow_tools,
        })
        item = self.steps.pop(0) if self.steps else self.step_handler
        if item is None:
            raise AssertionError("FakeBackend has no tool step queued")
        if callable(item) and not isinstance(item, StepResult):
            item = item(transcript, allow_tools)
        if isinstance(item, Exception):
            raise item
        return item
```

(keep the existing `complete` method unchanged).

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_llm_tools.py tests/test_llm.py -v` — Expected: all pass.

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/agents/base.py pulse/agents/llm.py tests/fakes.py tests/test_llm_tools.py
git commit -m "feat: provider-neutral tool loop with budget, limits and one run row" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: `tool_step` for the Anthropic and OpenAI backends

**Files:**
- Modify: `pulse/agents/providers/anthropic_backend.py`, `pulse/agents/providers/openai_backend.py`
- Test: `tests/test_anthropic_backend.py` (append), `tests/test_openai_backend.py` (append)

**Interfaces:**
- Consumes: `StepResult`, `ToolCall`, `ToolSpec` (Task 7).
- Produces: `AnthropicBackend.tool_step(...)` and `OpenAIBackend.tool_step(...)` implementing `ToolBackend`; module helpers `_anthropic_messages(transcript)` and `_openai_messages(system, transcript)`; each backend's API call and error mapping move into a private `_send(kwargs)` shared by `complete` and `tool_step`; usage extraction moves into a module-level `_usage(resp)` (Anthropic returns `(input, output, cache_read)`, OpenAI `(input, output, cached, reported)`).

Translation rules:
- Anthropic: user turn → `{"role": "user", "content": text}`; assistant turn → `{"role": "assistant", "content": [text block if text] + [tool_use blocks {"type": "tool_use", "id", "name", "input"}]}`; tool turn → one user message whose content is `tool_result` blocks `{"type": "tool_result", "tool_use_id", "content"}` plus a trailing text block for `note`. Tools → `{"name", "description", "input_schema"}`; `tool_choice` `{"type": "auto"}` or `{"type": "none"}` (Anthropic rejects transcripts with tool blocks unless tools are defined, so tools stay listed and `none` stops further calls).
- OpenAI/OpenRouter: system message first; user → `{"role": "user", "content"}`; assistant → `{"role": "assistant", "content": text, "tool_calls": [{"id", "type": "function", "function": {"name", "arguments": json.dumps(args)}}]}`; tool turn → one `{"role": "tool", "tool_call_id", "content"}` per result, then `{"role": "user", "content": note}` if a note is present. Tools → `{"type": "function", "function": {"name", "description", "parameters"}}`; `tool_choice` `"auto"` or `"none"`; OpenRouter adds `extra_body={"usage": {"include": True}}`. Tool arguments that are not a JSON object raise `OutputInvalid` carrying usage.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_anthropic_backend.py`:

```python
from pulse.agents.base import StepResult, ToolCall, ToolSpec

TOOLS = [ToolSpec("search_messages", "Find messages.", {"type": "object", "properties": {"text": {"type": "string"}}})]
TRANSCRIPT = [
    {"role": "user", "content": "why?"},
    {"role": "assistant", "text": "looking", "tool_calls": [ToolCall("tu1", "search_messages", {"text": "auth"})]},
    {"role": "tool", "results": [{"id": "tu1", "content": "[3 messages]"}], "note": "answer now"},
]


def test_tool_step_translates_transcript_and_parses_tool_use():
    resp = response([
        SimpleNamespace(type="text", text="Let me check threads."),
        SimpleNamespace(type="tool_use", id="tu2", name="search_messages", input={"text": "token"}),
    ])
    backend, messages = backend_for(resp)
    result = backend.tool_step("claude-sonnet-5", "sys", TRANSCRIPT, TOOLS)
    assert result == StepResult("Let me check threads.", (ToolCall("tu2", "search_messages", {"text": "token"}),), 150, 20, 900)
    kw = messages.kwargs
    assert kw["tools"] == [{"name": "search_messages", "description": "Find messages.", "input_schema": TOOLS[0].parameters}]
    assert kw["tool_choice"] == {"type": "auto"}
    assert kw["messages"] == [
        {"role": "user", "content": "why?"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "looking"},
            {"type": "tool_use", "id": "tu1", "name": "search_messages", "input": {"text": "auth"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1", "content": "[3 messages]"},
            {"type": "text", "text": "answer now"},
        ]},
    ]


def test_tool_step_final_answer_with_tools_disabled():
    backend, messages = backend_for(response([SimpleNamespace(type="text", text="Final.")]))
    result = backend.tool_step("m", "sys", TRANSCRIPT[:1], TOOLS, allow_tools=False)
    assert (result.text, result.tool_calls) == ("Final.", ())
    assert messages.kwargs["tool_choice"] == {"type": "none"}


def test_tool_step_maps_errors_like_complete():
    err = anthropic.RateLimitError("slow", response=httpx.Response(429, request=REQ), body=None)
    with pytest.raises(TransientError):
        backend_for(err)[0].tool_step("m", "sys", TRANSCRIPT[:1], TOOLS)
```

Append to `tests/test_openai_backend.py`:

```python
from pulse.agents.base import StepResult, ToolCall, ToolSpec

TOOLS = [ToolSpec("get_thread", "Get a thread.", {"type": "object", "properties": {"message_id": {"type": "string"}}})]
TRANSCRIPT = [
    {"role": "user", "content": "why?"},
    {"role": "assistant", "text": None, "tool_calls": [ToolCall("c1", "get_thread", {"message_id": "m1"})]},
    {"role": "tool", "results": [{"id": "c1", "content": "[thread]"}], "note": "answer now"},
]


def tool_response(content=None, calls=(), cost=None):
    message = SimpleNamespace(content=content, refusal=None, tool_calls=[
        SimpleNamespace(id=cid, type="function", function=SimpleNamespace(name=name, arguments=args))
        for cid, name, args in calls
    ] or None)
    usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=50,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=800))
    if cost is not None:
        usage.cost = cost
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def test_tool_step_translates_transcript_and_parses_calls():
    backend, completions = backend_for(tool_response(calls=[("c2", "get_thread", '{"message_id": "m2"}')]))
    result = backend.tool_step("gpt-x", "sys", TRANSCRIPT, TOOLS)
    assert result == StepResult(None, (ToolCall("c2", "get_thread", {"message_id": "m2"}),), 200, 50, 800, None)
    kw = completions.calls[0]
    assert kw["tools"] == [{"type": "function", "function": {"name": "get_thread", "description": "Get a thread.", "parameters": TOOLS[0].parameters}}]
    assert kw["tool_choice"] == "auto"
    assert kw["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "why?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "get_thread", "arguments": '{"message_id": "m1"}'}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "[thread]"},
        {"role": "user", "content": "answer now"},
    ]
    assert "response_format" not in kw


def test_tool_step_final_answer_with_tools_disabled_and_openrouter_cost():
    backend, completions = backend_for(tool_response(content="Final.", cost=0.003), openrouter=True)
    result = backend.tool_step("m", "sys", TRANSCRIPT[:1], TOOLS, allow_tools=False)
    assert (result.text, result.tool_calls, result.reported_cost) == ("Final.", (), 0.003)
    assert completions.calls[0]["tool_choice"] == "none"
    assert completions.calls[0]["extra_body"] == {"usage": {"include": True}}


def test_tool_step_bad_arguments_are_output_invalid():
    backend, _ = backend_for(tool_response(calls=[("c2", "get_thread", "{not json")]))
    with pytest.raises(OutputInvalid) as exc:
        backend.tool_step("gpt-x", "sys", TRANSCRIPT[:1], TOOLS)
    assert exc.value.output_tokens == 50
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_anthropic_backend.py tests/test_openai_backend.py -v`
Expected: FAIL (`AttributeError: 'AnthropicBackend' object has no attribute 'tool_step'`)

- [ ] **Step 3: Implement**

`pulse/agents/providers/anthropic_backend.py` — replace the module with:

```python
"""Anthropic backend: structured output via a single forced tool, and tool-loop steps."""
from __future__ import annotations

from typing import Any

import anthropic

from pulse.agents.base import (
    BackendResult, OutputInvalid, ProviderError, StepResult, ToolCall, ToolSpec, TransientError,
)


def _usage(resp) -> tuple[int, int, int]:
    usage = resp.usage
    # Cache writes are billed at the input rate (slight undercount of the write premium).
    input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", None) or 0)
    cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
    return input_tokens, usage.output_tokens or 0, cache_read


def _anthropic_messages(transcript: list[dict]) -> list[dict]:
    out = []
    for turn in transcript:
        role = turn["role"]
        if role == "user":
            out.append({"role": "user", "content": turn["content"]})
        elif role == "assistant":
            content: list[dict] = []
            if turn.get("text"):
                content.append({"type": "text", "text": turn["text"]})
            content += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in turn.get("tool_calls", [])
            ]
            out.append({"role": "assistant", "content": content})
        elif role == "tool":
            content = [
                {"type": "tool_result", "tool_use_id": r["id"], "content": r["content"]} for r in turn["results"]
            ]
            if turn.get("note"):
                content.append({"type": "text", "text": turn["note"]})
            out.append({"role": "user", "content": content})
        else:
            raise ValueError(f"unknown transcript role {role!r}")
    return out


class AnthropicBackend:
    def __init__(self, client=None, *, max_tokens: int = 8192):
        self._client = client if client is not None else anthropic.Anthropic()
        self._max_tokens = max_tokens

    def _system(self, system: str) -> list[dict]:
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    def _send(self, kwargs: dict[str, Any]):
        try:
            return self._client.messages.create(**kwargs)
        except anthropic.APIConnectionError as e:
            raise TransientError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise ProviderError(str(e)) from e

    def complete(self, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        resp = self._send(dict(
            model=model,
            max_tokens=self._max_tokens,
            system=self._system(system),
            messages=[{"role": "user", "content": user}],
            tools=[{"name": schema_name, "description": "Record the structured result.", "input_schema": schema}],
            tool_choice={"type": "tool", "name": schema_name},
        ))
        input_tokens, output_tokens, cache_read = _usage(resp)
        block = next((b for b in resp.content if b.type == "tool_use" and b.name == schema_name), None)
        if block is None:
            raise OutputInvalid("no tool_use block in response", input_tokens, output_tokens, cache_read)
        return BackendResult(dict(block.input), input_tokens, output_tokens, cache_read)

    def tool_step(
        self, model: str, system: str, transcript: list[dict], tools: list[ToolSpec], allow_tools: bool = True
    ) -> StepResult:
        kwargs: dict[str, Any] = dict(
            model=model, max_tokens=self._max_tokens, system=self._system(system),
            messages=_anthropic_messages(transcript),
        )
        if tools:
            kwargs["tools"] = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
            kwargs["tool_choice"] = {"type": "auto"} if allow_tools else {"type": "none"}
        resp = self._send(kwargs)
        input_tokens, output_tokens, cache_read = _usage(resp)
        text = "".join(b.text for b in resp.content if b.type == "text") or None
        calls = tuple(ToolCall(b.id, b.name, dict(b.input)) for b in resp.content if b.type == "tool_use")
        return StepResult(text, calls, input_tokens, output_tokens, cache_read)
```

`pulse/agents/providers/openai_backend.py`:
- import `StepResult`, `ToolCall`, `ToolSpec` from `pulse.agents.base`;
- add module-level helpers:

```python
def _usage(resp) -> tuple[int, int, int, float | None]:
    usage = resp.usage
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", None) or 0) if details is not None else 0
    cost = getattr(usage, "cost", None)
    return (usage.prompt_tokens or 0) - cached, usage.completion_tokens or 0, cached, (
        float(cost) if cost is not None else None
    )


def _openai_messages(system: str, transcript: list[dict]) -> list[dict]:
    out: list[dict] = [{"role": "system", "content": system}]
    for turn in transcript:
        role = turn["role"]
        if role == "user":
            out.append({"role": "user", "content": turn["content"]})
        elif role == "assistant":
            message: dict = {"role": "assistant", "content": turn.get("text")}
            if turn.get("tool_calls"):
                message["tool_calls"] = [
                    {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                    for c in turn["tool_calls"]
                ]
            out.append(message)
        elif role == "tool":
            out.extend(
                {"role": "tool", "tool_call_id": r["id"], "content": r["content"]} for r in turn["results"]
            )
            if turn.get("note"):
                out.append({"role": "user", "content": turn["note"]})
        else:
            raise ValueError(f"unknown transcript role {role!r}")
    return out
```

- split `_create` into building the kwargs and a new `_send(kwargs)` that holds the existing `try/except` error mapping (so `_create` ends with `return self._send(kwargs)`);
- change `_parse` to start with `input_tokens, output_tokens, cached, reported = _usage(resp)` instead of computing them inline;
- add:

```python
    def tool_step(
        self, model: str, system: str, transcript: list[dict], tools: list[ToolSpec], allow_tools: bool = True
    ) -> StepResult:
        kwargs: dict[str, Any] = {"model": model, "messages": _openai_messages(system, transcript)}
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
            kwargs["tool_choice"] = "auto" if allow_tools else "none"
        if self._openrouter:
            kwargs["extra_body"] = {"usage": {"include": True}}
        resp = self._send(kwargs)
        input_tokens, output_tokens, cached, reported = _usage(resp)
        message = resp.choices[0].message
        calls = []
        for tc in getattr(message, "tool_calls", None) or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                raise OutputInvalid(
                    f"tool arguments not JSON: {e}", input_tokens, output_tokens, cached, reported
                ) from e
            if not isinstance(args, dict):
                raise OutputInvalid("tool arguments must be a JSON object", input_tokens, output_tokens, cached, reported)
            calls.append(ToolCall(tc.id, tc.function.name, args))
        return StepResult(message.content, tuple(calls), input_tokens, output_tokens, cached, reported)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_anthropic_backend.py tests/test_openai_backend.py -v` — Expected: all pass (existing tests unchanged).

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/agents/providers/anthropic_backend.py pulse/agents/providers/openai_backend.py tests/test_anthropic_backend.py tests/test_openai_backend.py
git commit -m "feat: tool_step for Anthropic, OpenAI and OpenRouter backends" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Investigate agent

**Files:**
- Create: `pulse/agents/investigate.py`, `pulse/agents/prompts/investigate_v1.md`
- Test: `tests/test_investigate.py`

**Interfaces:**
- Consumes: `LLMClient.run_tools`, `ToolSpec`, `ToolCall`, `ToolError` (Task 7); `stats` (Task 2); `strip_unknown`, `cited_ids` (Task 1); `KINDS`.
- Produces:
  - `TOOLS: list[ToolSpec]` named `query_stats`, `search_messages`, `get_thread`; `MAX_TOOL_CALLS = 12`; `SEARCH_LIMIT_MAX = 50`; `THREAD_LIMIT = 30`
  - `class Toolbox(conn, now)` with `.seen: set[str]` (every message id returned to the model) and `execute(call: ToolCall) -> str` (JSON string; raises `ToolError` for unknown tools or bad arguments)
  - `@dataclass(frozen=True) InvestigationResult(investigation_id: int, markdown: str, cited_message_ids: list[str], removed_citations: list[str], tool_calls: int)`
  - `run_investigation(conn, llm, question: str, now, *, context: dict | None = None) -> InvestigationResult` — inserts the `investigations` row first; on success fills `markdown`, `cited_message_ids`, `run_id`; on `LLMError`/`BudgetExceeded` stores `_Investigation failed: <error>_` and re-raises; `ValueError` for an empty question.

Tool semantics: dates are `YYYY-MM-DD` (UTC). `query_stats(metric, start?, end?)` with metrics `period_summary | sentiment_series | theme_scores | queue_counts`; `end` defaults to tomorrow, `start` to 7 days before `end`. `search_messages(text?, theme_id?, author_id?, kind?, start?, end?, negative_only?, limit?)` searches triaged messages (all time by default), newest first, `limit` clamped to 1..50 (default 20), content clipped to 300 chars. `get_thread(message_id)` returns the message, its thread (or the thread started from it) and direct replies, oldest first, at most 30.

- [ ] **Step 1: Write the prompt** `pulse/agents/prompts/investigate_v1.md`:

```markdown
You investigate questions about a developer product's Discord community for the DevRel team, such as "why did sentiment dip on Tuesday?" or "what is driving the auth docs complaints?".

You receive JSON with the question, optional context (for example a theme id), and today's date. Use the tools to look at the data before answering:
- query_stats for counts, sentiment over time, ranked pain points and the mod queue;
- search_messages to find the messages behind a trend;
- get_thread to read a conversation in full.

Then write a short markdown answer:
- Start with the answer in one or two sentences, with the numbers that support it.
- Then 2-5 bullets of evidence. Cite each message you rely on exactly as [[msg:<message_id>]], using only message_ids returned by your tool calls.
- End with one line on what the team could do next.

Rules: do not invent messages, quotes or numbers. If the data does not answer the question, say what is missing. Keep it under 250 words. Use at most a handful of tool calls.
```

- [ ] **Step 2: Write the failing tests** — `tests/test_investigate.py`:

```python
import json
from datetime import timedelta

import pytest

from pulse.agents.base import LLMError, ProviderError, StepResult, ToolCall, ToolError
from pulse.agents.investigate import SEARCH_LIMIT_MAX, Toolbox, run_investigation
from pulse.db import connect
from pulse.store import upsert_messages
from pulse.themes import assign, create_theme
from tests.fakes import T0, FakeBackend, make_config, make_llm, msg, set_triage

NOW = T0 + timedelta(days=1)


def seed():
    conn = connect(":memory:")
    upsert_messages(conn, [
        msg("q1", "Auth token exchange is not documented", minutes=0),
        msg("r1", "same here, stuck on auth", minutes=5, author_id="u2", author_name="bob", reply_to_id="q1"),
        msg("t1", "thread reply about auth", minutes=6, thread_id="q1", author_id="u3", author_name="cy"),
        msg("p1", "love the new CLI", minutes=10),
    ], frozenset())
    set_triage(conn, "q1", sentiment=-1, kind="docs", topics=["auth docs"])
    set_triage(conn, "r1", sentiment=-1, kind="docs", topics=["auth docs"])
    set_triage(conn, "t1", sentiment=0, kind="other")
    set_triage(conn, "p1", sentiment=2, kind="praise", topics=["cli"])
    with conn:
        tid, _ = create_theme(conn, "Auth docs gaps", "token step", T0)
        assign(conn, "q1", tid)
        assign(conn, "r1", tid)
    return conn, tid


def call(tool, **args):
    return ToolCall("c", tool, args)


def test_search_messages_filters_and_records_seen_ids():
    conn, tid = seed()
    box = Toolbox(conn, NOW)
    found = json.loads(box.execute(call("search_messages", text="AUTH")))
    assert [m["message_id"] for m in found] == ["t1", "r1", "q1"]
    assert [m["message_id"] for m in json.loads(box.execute(call("search_messages", theme_id=tid)))] == ["r1", "q1"]
    assert [m["message_id"] for m in json.loads(box.execute(call("search_messages", kind="praise")))] == ["p1"]
    assert [m["message_id"] for m in json.loads(box.execute(call("search_messages", negative_only=True)))] == ["r1", "q1"]
    assert box.seen == {"q1", "r1", "t1", "p1"}


def test_search_limit_is_clamped():
    conn, _ = seed()
    box = Toolbox(conn, NOW)
    assert len(json.loads(box.execute(call("search_messages", limit=1000)))) == 4
    assert len(json.loads(box.execute(call("search_messages", limit=0)))) == 1
    assert SEARCH_LIMIT_MAX == 50


def test_get_thread_includes_replies_and_thread_started_from_message():
    conn, _ = seed()
    box = Toolbox(conn, NOW)
    thread = json.loads(box.execute(call("get_thread", message_id="q1")))
    assert [m["message_id"] for m in thread] == ["q1", "r1", "t1"]


def test_query_stats_metrics():
    conn, tid = seed()
    box = Toolbox(conn, NOW)
    summary = json.loads(box.execute(call("query_stats", metric="period_summary", start="2026-09-28", end="2026-09-29")))
    assert summary["messages"] == 4
    scores = json.loads(box.execute(call("query_stats", metric="theme_scores")))
    assert scores[0]["name"] == "Auth docs gaps"
    assert json.loads(box.execute(call("query_stats", metric="queue_counts"))) == {"open": 0, "frustrated": 0, "unanswered": 0}


def test_toolbox_rejects_bad_arguments():
    conn, _ = seed()
    box = Toolbox(conn, NOW)
    for bad in (
        call("drop_tables"),
        call("query_stats", metric="everything"),
        call("query_stats", metric="period_summary", start="Sept 1"),
        call("query_stats", metric="period_summary", start="2026-09-29", end="2026-09-28"),
        call("search_messages", kind="rant"),
        call("search_messages", limit="ten"),
        call("get_thread", message_id="nope"),
        call("get_thread"),
    ):
        with pytest.raises(ToolError):
            box.execute(bad)


def test_investigation_strips_citations_to_unseen_messages():
    conn, _ = seed()
    backend = FakeBackend(steps=[
        StepResult(None, (ToolCall("c1", "search_messages", {"text": "auth"}),), 100, 10),
        StepResult("Auth docs skip the token step [[msg:q1]] [[msg:p1]] [[msg:ghost]].", (), 100, 50),
    ])
    result = run_investigation(conn, make_llm(conn, make_config(), backend), "why is auth painful?", NOW)
    assert result.cited_message_ids == ["q1"]
    assert result.removed_citations == ["p1", "ghost"]
    assert result.tool_calls == 1
    row = conn.execute("SELECT * FROM investigations WHERE id = ?", (result.investigation_id,)).fetchone()
    assert row["question"] == "why is auth painful?"
    assert json.loads(row["cited_message_ids"]) == ["q1"]
    assert row["run_id"] is not None
    assert "search_messages" in backend.step_calls[0]["tools"]


def test_failed_investigation_is_recorded_and_raised():
    conn, _ = seed()
    backend = FakeBackend(steps=[ProviderError("401 bad key")])
    with pytest.raises(LLMError):
        run_investigation(conn, make_llm(conn, make_config(), backend), "why?", NOW, context={"theme_id": 1})
    row = conn.execute("SELECT * FROM investigations").fetchone()
    assert row["markdown"].startswith("_Investigation failed:")
    assert json.loads(row["context"]) == {"theme_id": 1}


def test_empty_question_is_rejected():
    conn, _ = seed()
    with pytest.raises(ValueError):
        run_investigation(conn, make_llm(conn, make_config(), FakeBackend()), "  ", NOW)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_investigate.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.agents.investigate'`

- [ ] **Step 4: Implement** `pulse/agents/investigate.py`:

```python
"""Investigate agent: answers "why" questions with read-only tools over the database."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from pulse import stats
from pulse.agents.base import BudgetExceeded, LLMError, ToolCall, ToolError, ToolSpec
from pulse.agents.llm import LLMClient
from pulse.citations import cited_ids, strip_unknown
from pulse.models import KINDS, to_iso

MAX_TOOL_CALLS = 12
SEARCH_LIMIT_MAX = 50
THREAD_LIMIT = 30
_CLIP = 300
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_DATE = {"type": "string", "description": "YYYY-MM-DD (UTC)"}

TOOLS = [
    ToolSpec(
        "query_stats",
        "Aggregate statistics for a date range [start, end). metric: period_summary (message count, average "
        "sentiment, negatives, needs-reply, counts by kind), sentiment_series (per day), theme_scores (pain "
        "points ranked, for the window ending at end), queue_counts (open mod queue items). Defaults: end = "
        "tomorrow, start = 7 days before end.",
        {"type": "object", "properties": {
            "metric": {"type": "string", "enum": ["period_summary", "sentiment_series", "theme_scores", "queue_counts"]},
            "start": _DATE, "end": _DATE,
        }, "required": ["metric"]},
    ),
    ToolSpec(
        "search_messages",
        "Find triaged community messages, newest first. All filters are optional. Each result has a "
        "message_id you can cite.",
        {"type": "object", "properties": {
            "text": {"type": "string", "description": "case-insensitive substring of the message text"},
            "theme_id": {"type": "integer"},
            "author_id": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "start": _DATE, "end": _DATE,
            "negative_only": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT_MAX},
        }},
    ),
    ToolSpec(
        "get_thread",
        "The conversation around one message: the message, its thread (or the thread started from it) and "
        "direct replies, oldest first.",
        {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
    ),
]


def _clip(text: str) -> str:
    return text if len(text) <= _CLIP else text[:_CLIP] + "…"


class Toolbox:
    def __init__(self, conn: sqlite3.Connection, now: datetime):
        self.conn = conn
        self.now = now
        self.seen: set[str] = set()

    def execute(self, call: ToolCall) -> str:
        handlers = {
            "query_stats": self.query_stats,
            "search_messages": self.search_messages,
            "get_thread": self.get_thread,
        }
        handler = handlers.get(call.name)
        if handler is None:
            raise ToolError(f"unknown tool {call.name!r}")
        return json.dumps(handler(call.arguments), ensure_ascii=False, default=str)

    def _tomorrow(self) -> datetime:
        return (self.now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    def _date(self, args: dict, key: str, default: datetime) -> datetime:
        value = args.get(key)
        if value in (None, ""):
            return default
        try:
            return datetime.combine(date.fromisoformat(str(value)), time.min, timezone.utc)
        except ValueError as e:
            raise ToolError(f"{key} must be YYYY-MM-DD, got {value!r}") from e

    def _range(self, args: dict, default_start: datetime | None = None) -> tuple[datetime, datetime]:
        end = self._date(args, "end", self._tomorrow())
        start = self._date(args, "start", default_start if default_start is not None else end - timedelta(days=7))
        if start >= end:
            raise ToolError("start must be before end")
        return start, end

    def _int(self, args: dict, key: str) -> int | None:
        value = args.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{key} must be an integer, got {value!r}")
        return value

    def query_stats(self, args: dict):
        metric = args.get("metric")
        start, end = self._range(args)
        if metric == "period_summary":
            return stats.period_summary(self.conn, start, end)
        if metric == "sentiment_series":
            return stats.sentiment_series(self.conn, start, end)
        if metric == "theme_scores":
            window = max(1, (end - start).days)
            return [asdict(s) for s in stats.theme_scores(self.conn, end, window_days=window)]
        if metric == "queue_counts":
            return stats.queue_counts(self.conn)
        raise ToolError(f"unknown metric {metric!r}")

    def search_messages(self, args: dict):
        start, end = self._range(args, default_start=_EPOCH)
        limit = self._int(args, "limit")
        limit = 20 if limit is None else max(1, min(limit, SEARCH_LIMIT_MAX))
        kind = args.get("kind")
        if kind is not None and kind not in KINDS:
            raise ToolError(f"kind must be one of {list(KINDS)}")
        sql = (
            f"SELECT DISTINCT {stats.MESSAGE_COLUMNS}, m.author_id FROM messages m"
            " JOIN triage t ON t.message_id = m.id"
        )
        params: list = []
        theme_id = self._int(args, "theme_id")
        if theme_id is not None:
            ids = stats.theme_member_ids(self.conn, theme_id)
            if not ids:
                return []
            sql += f" JOIN message_themes mt ON mt.message_id = m.id AND mt.theme_id IN ({','.join('?' * len(ids))})"
            params += ids
        sql += " WHERE m.is_bot = 0 AND m.created_at >= ? AND m.created_at < ?"
        params += [to_iso(start), to_iso(end)]
        if args.get("text"):
            sql += " AND lower(m.content) LIKE ?"
            params.append(f"%{str(args['text']).lower()}%")
        if args.get("author_id"):
            sql += " AND m.author_id = ?"
            params.append(str(args["author_id"]))
        if kind is not None:
            sql += " AND t.kind = ?"
            params.append(kind)
        if args.get("negative_only") is True:
            sql += " AND t.sentiment < 0"
        sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
        params.append(limit)
        results = []
        for r in self.conn.execute(sql, params):
            item = stats.to_message(r)
            item["content"] = _clip(item["content"])
            item["author_id"] = r["author_id"]
            results.append(item)
        self.seen.update(m["message_id"] for m in results)
        return results

    def get_thread(self, args: dict):
        mid = args.get("message_id")
        if not isinstance(mid, str) or not mid:
            raise ToolError("message_id is required")
        root = self.conn.execute("SELECT id, thread_id FROM messages WHERE id = ?", (mid,)).fetchone()
        if root is None:
            raise ToolError(f"no message {mid!r}")
        thread = root["thread_id"] or mid
        rows = self.conn.execute(
            "SELECT m.id, m.author_name, m.is_team, m.created_at, m.content FROM messages m"
            " WHERE m.id = ? OR m.thread_id = ? OR m.reply_to_id = ?"
            " ORDER BY m.created_at, m.id LIMIT ?",
            (mid, thread, mid, THREAD_LIMIT),
        ).fetchall()
        results = [
            {"message_id": r["id"], "author": r["author_name"], "is_team": bool(r["is_team"]),
             "created_at": r["created_at"], "content": _clip(r["content"])}
            for r in rows
        ]
        self.seen.update(m["message_id"] for m in results)
        return results


@dataclass(frozen=True)
class InvestigationResult:
    investigation_id: int
    markdown: str
    cited_message_ids: list[str]
    removed_citations: list[str]
    tool_calls: int


def load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "investigate_v1.md").read_text(encoding="utf-8")


def run_investigation(
    conn: sqlite3.Connection, llm: LLMClient, question: str, now: datetime, *, context: dict | None = None
) -> InvestigationResult:
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    context = context or {}
    with conn:
        inv_id = int(conn.execute(
            "INSERT INTO investigations (question, context, created_at) VALUES (?, ?, ?)",
            (question, json.dumps(context), to_iso(now)),
        ).lastrowid)
    toolbox = Toolbox(conn, now)
    user = json.dumps({"question": question, "context": context, "today": now.date().isoformat()})
    try:
        resp = llm.run_tools("investigate", load_prompt(), user, TOOLS, toolbox.execute, max_calls=MAX_TOOL_CALLS)
    except (BudgetExceeded, LLMError) as e:
        with conn:
            conn.execute(
                "UPDATE investigations SET markdown = ? WHERE id = ?", (f"_Investigation failed: {e}_", inv_id)
            )
        raise
    markdown, removed = strip_unknown(resp.text, toolbox.seen)
    cited = cited_ids(markdown)
    with conn:
        conn.execute(
            "UPDATE investigations SET markdown = ?, cited_message_ids = ?, run_id = ? WHERE id = ?",
            (markdown, json.dumps(cited), resp.run_id, inv_id),
        )
    return InvestigationResult(inv_id, markdown, cited, removed, resp.tool_calls)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_investigate.py -v` — Expected: 8 passed.

- [ ] **Step 6: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/agents/investigate.py pulse/agents/prompts/investigate_v1.md tests/test_investigate.py
git commit -m "feat: Investigate agent with read-only tools and verified citations" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: CLI commands, pipeline wiring, docs

**Files:**
- Modify: `pulse/pipeline.py`, `pulse/run.py`, `README.md`
- Test: `tests/test_pipeline.py` (append), `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `run_themes`, `ThemeStats` (Task 5); `run_digest`, `DigestResult` (Task 6); `run_investigation`, `InvestigationResult` (Task 9); `sync_launches`, `render_text` (Task 1).
- Produces:
  - `PipelineReport` gains `themes: ThemeStats | None = None` (last field); `run_pipeline` now runs ingest → `sync_launches` → triage → themes → mod queue.
  - `format_themes(stats: ThemeStats) -> str`, `format_digest(result: DigestResult, conn) -> str`, `format_investigation(result: InvestigationResult, conn) -> str`; `format_report` includes the themes line when present.
  - CLI: `themes`; `digest [--launch NAME]`; `investigate QUESTION [--theme ID]`. Every command syncs `[[launches]]` after connecting. `digest`/`investigate` print errors to stderr and exit 1 on `LookupError`, `BudgetExceeded` or `LLMError`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pipeline.py` (put new imports with the existing ones at the top of the file):

```python
from pulse.agents.digest import DigestResult
from pulse.agents.investigate import InvestigationResult
from pulse.agents.theme import ThemeStats
from pulse.pipeline import format_digest, format_investigation, format_themes


def label_or_theme(user):
    data = json.loads(user)
    if "themes" in data:
        ids = [m["message_id"] for m in data["messages"]]
        return BackendResult({"assignments": [], "merges": [], "renames": [],
                              "new_themes": [{"name": "Install", "description": "d", "message_ids": ids}]}, 10, 10)
    return label(user)


def test_pipeline_themes_triaged_messages(tmp_path):
    for name in ("dce_channel.json", "dce_thread.json", "messages.csv"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    conn = connect(":memory:")
    config = make_config(imports_dir=tmp_path)
    report = run_pipeline(conn, config, make_llm(conn, config, FakeBackend(handler=label_or_theme)),
                          source=FileSource(tmp_path), now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc))
    assert report.themes.created == 1
    assert report.themes.assigned == report.themes.considered > 0
    assert "themes:" in format_report(report)


def test_format_themes_lists_rejections_and_budget():
    text = format_themes(ThemeStats(considered=5, jev_assigned=2, llm_batches=1, created=1, assigned=3,
                                    rejected=["merge 1->2: both themes must be active and different"],
                                    skipped_budget=True))
    assert "considered 5" in text and "jev assigned 2" in text and "new themes 1" in text
    assert "rejected: merge 1->2" in text
    assert "daily budget cap reached" in text


def test_format_digest_and_investigation_render_citations():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", "hi", author_name="alice")], frozenset())
    digest = DigestResult(7, "weekly", "2026-09-21T00:00:00.000000Z", "2026-09-28T00:00:00.000000Z",
                          "Good [[msg:m1]].", ["m1"], ["ghost"])
    text = format_digest(digest, conn)
    assert text.startswith("digest #7 (weekly")
    assert "(alice, https://discord.com/channels/900/100/m1)" in text
    assert "removed 1 citation" in text
    inv = InvestigationResult(3, "Because [[msg:m1]].", ["m1"], [], 2)
    assert "investigation #3 (2 tool calls)" in format_investigation(inv, conn)
```

Append to `tests/test_cli.py`:

```python
LAUNCH_CONFIG = CONFIG + '''
[[launches]]
name = "v2.0 SDK"
date = "2026-09-15"
keywords = ["v2"]
'''


def test_commands_sync_launches_and_themes_runs_with_nothing_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(LAUNCH_CONFIG)
    assert main(["--config", str(tmp_path / "pulse.toml"), "themes"]) == 0
    assert "themes: considered 0" in capsys.readouterr().out
    assert connect(tmp_path / "pulse.db").execute("SELECT name FROM launches").fetchone()[0] == "v2.0 SDK"


def test_digest_unknown_launch_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(LAUNCH_CONFIG)
    assert main(["--config", str(tmp_path / "pulse.toml"), "digest", "--launch", "nope"]) == 1
    assert "unknown launch 'nope'" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'format_digest'`

- [ ] **Step 3: Implement**

`pulse/pipeline.py`:
- imports: `from pulse.agents.digest import DigestResult`, `from pulse.agents.investigate import InvestigationResult`, `from pulse.agents.theme import ThemeStats, run_themes`, `from pulse.citations import render_text`, and add `sync_launches` to the `pulse.store` import;
- `PipelineReport`: add `themes: ThemeStats | None = None` as the last field;
- `run_pipeline`: after `ingest(...)` call `sync_launches(conn, config.launches)`; after `run_triage` call `theme_stats = run_themes(conn, llm, now)`; pass `themes=theme_stats` into `PipelineReport`;
- add:

```python
def format_themes(stats: ThemeStats) -> str:
    lines = [
        f"themes: considered {stats.considered}, jev assigned {stats.jev_assigned},"
        f" llm batches {stats.llm_batches} (failed {stats.failed_batches}), new themes {stats.created},"
        f" assignments {stats.assigned}, merges {stats.merged}, renames {stats.renamed}"
    ]
    lines += [f"  rejected: {r}" for r in stats.rejected]
    if stats.skipped_budget:
        lines.append("  daily budget cap reached: remaining messages will be themed on the next run")
    return "\n".join(lines)


def _removed_note(removed: list[str]) -> str:
    return f"\n\n(removed {len(removed)} citation(s) to messages the agent was not shown)" if removed else ""


def format_digest(result: DigestResult, conn: sqlite3.Connection) -> str:
    header = f"digest #{result.digest_id} ({result.kind}, {result.period_start[:10]} to {result.period_end[:10]})"
    return f"{header}\n\n{render_text(result.markdown, conn)}{_removed_note(result.removed_citations)}"


def format_investigation(result: InvestigationResult, conn: sqlite3.Connection) -> str:
    header = f"investigation #{result.investigation_id} ({result.tool_calls} tool calls)"
    return f"{header}\n\n{render_text(result.markdown, conn)}{_removed_note(result.removed_citations)}"
```

- in `format_report`, append `format_themes(report.themes)` to the list when `report.themes is not None` (between the triage and mod queue lines).

`pulse/run.py`:
- imports: `from pulse.agents.base import BudgetExceeded, LLMError`, `from pulse.agents.digest import run_digest`, `from pulse.agents.investigate import run_investigation`, `from pulse.agents.theme import run_themes`, `from pulse.store import sync_launches`, and add `format_digest, format_investigation, format_themes` to the `pulse.pipeline` import;
- in `_parser`, add:

```python
    sub.add_parser("themes", help="group labelled messages into recurring themes")
    digest = sub.add_parser("digest", help="write the weekly digest, or a launch digest with --launch")
    digest.add_argument("--launch", help="launch name from [[launches]] in pulse.toml")
    investigate = sub.add_parser("investigate", help="ask the Investigate agent a question")
    investigate.add_argument("question")
    investigate.add_argument("--theme", type=int, help="theme id to focus on")
```

- in `main`, right after `conn = connect(config.db_path)`: `sync_launches(conn, config.launches)`;
- add branches:

```python
    elif args.command == "themes":
        print(format_themes(run_themes(conn, build_llm(conn, config), now)))
    elif args.command == "digest":
        try:
            result = run_digest(conn, build_llm(conn, config), now, launch=args.launch)
        except LookupError as e:
            print(f"digest: {e.args[0]}", file=sys.stderr)
            return 1
        except (BudgetExceeded, LLMError) as e:
            print(f"digest failed: {e}", file=sys.stderr)
            return 1
        print(format_digest(result, conn))
    elif args.command == "investigate":
        context = {"theme_id": args.theme} if args.theme is not None else None
        try:
            result = run_investigation(conn, build_llm(conn, config), args.question, now, context=context)
        except (BudgetExceeded, LLMError, ValueError) as e:
            print(f"investigation failed: {e}", file=sys.stderr)
            return 1
        print(format_investigation(result, conn))
```

`README.md` — add after the Jev section:

````markdown
## Themes, digests, investigations

```bash
.venv/bin/python -m pulse.run themes                         # group labelled messages into pain points
.venv/bin/python -m pulse.run digest                         # weekly digest
.venv/bin/python -m pulse.run digest --launch "v2.0 SDK"     # before/after a [[launches]] entry
.venv/bin/python -m pulse.run investigate "why did sentiment dip on Tuesday?"
```

`pipeline` now also runs `themes`. Themes are proposed by the `theme` model (with Jev assigning messages to existing themes when the classifier is enabled); a run creates at most 5 new themes and 3 merges, and every change is logged. Digests and investigations cite real messages; the CLI prints each citation as the author and a link to the message, and drops any citation to a message the agent was not shown.
````

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_cli.py -v` — Expected: all pass.

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/pipeline.py pulse/run.py README.md tests/test_pipeline.py tests/test_cli.py
git commit -m "feat: themes, digest and investigate commands; themes in the pipeline" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
