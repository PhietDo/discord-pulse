# Discord Pulse Plan 4: Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the approved dashboard on localhost (Overview, Pain points, Bugs, Mod queue, Messages, Launch, Reports, Runs) over the existing SQLite data, with reply-time stats, pain point status, channel and window filters, dashboard-launched Investigate and digests, and a no-keys demo mode.

**Architecture:** FastAPI app factory (`pulse/web/app.py`) with one router per view, Jinja templates styled by the CSS of the approved mockup, and server-drawn SVG charts. Views read through `pulse.stats` plus small web-only queries; every request opens its own SQLite connection. Agent work started from the dashboard runs as a FastAPI background task on its own connection, and the page polls with htmx until the result is stored. `seed-demo` writes a deterministic synthetic community to `demo.db` and `web --demo` serves it without API keys.

**Tech Stack:** Python 3.12, FastAPI, Jinja2, htmx 2 (unpkg), python-markdown, uvicorn, sqlite3, pytest + FastAPI TestClient.

**Spec:** `docs/superpowers/specs/2026-09-29-discord-pulse-design.md` (sections 2, 4, 5, 8, 9, 10, 11, and 15 for the dashboard decisions). Visual spec: `docs/design/dashboard-mockup.html`.

**Plan series:** Plans 1-3 done. Plan 4 (this) = dashboard + demo. Plan 5 = eval harness (Jev gate), bot adapter, bot pitch, launchd, GitHub/Linear issue export, Slack alerts.

## Global Constraints

- Python `>=3.12`; run tests with `.venv/bin/pytest`; tests never touch the network (fakes, TestClient only).
- All stored timestamps come from `pulse.models.to_iso` (fixed-width UTC); read them back with `from_iso`. Windows are `[start, end)` in UTC.
- Secrets only from env vars. `web --demo` and `seed-demo` need no API keys.
- Every commit message ends with a blank line then exactly `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- The look comes from `docs/design/dashboard-mockup.html`: its CSS (copied into `pulse/web/static/pulse.css` in Task 4), its layout and its message card.
- Every message the dashboard shows is rendered by the `message_card` macro: avatar, author, a "team" pill for staff, `#channel`, age (full UTC time on hover), the text (escaped; over 400 characters it shows the first 400 and a "Show all" expander), kind and sentiment chips, an "Open in Discord ↗" link to `https://discord.com/channels/{guild_id}/{channel_id}/{message_id}` (built by `pulse.links.jump_link`), and an "All from {author}" link to `/messages?author_id=...` that keeps the current filters.
- Filters: every view accepts `days` in {7, 14, 30, 90} (default 14) and `channel` (a top-level channel id; empty means all). Invalid values fall back to the defaults, never an error. Every internal link and form keeps the current filters through `Filters.qs()`.
- A channel filter includes that channel's threads (`m.channel_id IN (...) OR m.parent_channel_id IN (...)`).
- Each request opens its own connection with `pulse.db.connect(settings.db_path)` and closes it at the end of the request.
- Jinja autoescaping stays on. Message text is always escaped. Agent markdown is rendered only through `pulse.citations.render_html`, which escapes HTML before converting markdown.
- The pain point score is unchanged (spec section 6): a fixed 7-day window, inside the selected channel.
- Agent work started from the dashboard (digest, Investigate) runs as a FastAPI background task with its own connection and `settings.llm_factory(conn, config)`. When agents are off (`settings.agents_on` is false: demo mode or no factory), agent buttons render disabled with the text "Agents are off in demo mode" (demo) or "Agents are off" (no factory).
- Spec clarifications from section 15 apply (server-side SVG charts instead of Chart.js; `messages.parent_channel_id`; `theme_status`; `removed_citations` on digests and investigations).

## Review Focus

1. A brand-new install (no messages, no themes, no runs, no digests) must render every view with an empty state, never a 500 (each view task has a `test_*_empty_db` test).
2. Message text and agent markdown containing HTML (`<script>`, `<img onerror>`) render as text, never as markup (Task 4 `test_render_html_escapes_raw_html_and_links_citations`, `test_message_card_escapes_content_and_links`).
3. Bad query parameters (`days=abc`, `days=5`, unknown `channel`, `theme=999`, `page=-3`, non-numeric ids) fall back to defaults or a friendly empty state, never a 500 (Task 4 `test_parse_filters_falls_back`, Task 6 `test_pain_unknown_theme_falls_back`, Task 8 `test_messages_bad_params`).
4. Closing a mod queue item that is already closed or does not exist returns 404 with a short message and changes nothing (Task 7 `test_close_already_closed_item_is_404`).
5. A dashboard Investigate whose agent fails must show the failure note, not poll forever (Task 10 `test_failed_investigation_stops_polling`).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `pulse/db.py` | `messages.parent_channel_id`, `theme_status` table, `removed_citations` columns, generic `_ADDED_COLUMNS` migration | 1, 3, 9 |
| `pulse/store.py` | store `parent_channel_id` | 1 |
| `pulse/stats.py` | channel scope, `top_channels`, reply-time stats, `channel_breakdown`, theme scope for Investigate | 1, 2, 10 |
| `pulse/theme_status.py` | pain point status and before/after comparison | 3 |
| `pulse/citations.py` | `render_html` | 4 |
| `pulse/modqueue.py` | `list_open(channels=)` with `queue_id`, `close_item` | 5, 7 |
| `pulse/web/settings.py`, `filters.py`, `cards.py`, `fmt.py`, `context.py`, `deps.py`, `app.py` | web foundation | 4 |
| `pulse/web/charts.py`, `pulse/web/queries.py` | SVG charts, web-only queries | 5 (queries grows in 6-11) |
| `pulse/web/views/*.py` | one router per view | 4-11 |
| `pulse/web/jobs.py` | background digest and Investigate jobs | 9, 10 |
| `pulse/web/templates/*.html`, `pulse/web/static/pulse.css` | templates and CSS | 4-11 |
| `pulse/agents/digest.py`, `pulse/agents/investigate.py` | store `removed_citations`; `investigation_id=`; `query_stats` theme/channel; `get_thread` window; empty-window digest counts queue items | 9, 10, 13 |
| `pulse/demo.py` | deterministic demo dataset and demo config | 12 |
| `pulse/run.py`, `README.md`, `pyproject.toml` | `web`, `seed-demo`, docs, deps | 4, 12, 13 |
| `tests/web_fakes.py` | TestClient factory and a seeded small community | 4, 5 |

---

### Task 1: Channel scoping in the data layer

**Files:**
- Modify: `pulse/db.py`, `pulse/store.py`, `pulse/stats.py`
- Test: `tests/test_db.py` (append), `tests/test_store.py` (append), `tests/test_stats.py` (append)

**Interfaces:**
- Produces:
  - `messages.parent_channel_id TEXT` (in `CREATE TABLE messages` and migrated for older databases).
  - `pulse.db._ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]]` (replaces `_TRIAGE_ADDED_COLUMNS`; later tasks add tables to it).
  - `upsert_messages` stores `Message.parent_channel_id`.
  - `pulse.stats.scope_clause(channels: tuple[str, ...] | None) -> tuple[str, list]`
  - keyword `channels: tuple[str, ...] | None = None` on `stats.period_summary`, `stats.sentiment_series`, `stats.theme_scores`, `stats.sample_messages`.
  - `pulse.stats.top_channels(conn) -> list[dict]` with keys `id, name, messages` (top-level channels only, busiest first).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
def test_old_messages_table_gains_parent_channel_id(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,"
        " channel_name TEXT NOT NULL DEFAULT '', thread_id TEXT, author_id TEXT NOT NULL,"
        " author_name TEXT NOT NULL, author_avatar_url TEXT, is_team INTEGER NOT NULL DEFAULT 0,"
        " is_bot INTEGER NOT NULL DEFAULT 0, content TEXT NOT NULL, created_at TEXT NOT NULL,"
        " edited_at TEXT, reply_to_id TEXT, source TEXT NOT NULL);"
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at, source)"
        " VALUES ('m1', '900', '100', 'u1', 'alice', 'hi', '2026-09-28T12:00:00.000000Z', 'file');"
    )
    raw.commit()
    raw.close()
    conn = connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    assert "parent_channel_id" in cols
    assert conn.execute("SELECT parent_channel_id FROM messages WHERE id = 'm1'").fetchone()[0] is None
    conn.close()
    connect(path).close()  # migrating twice is a no-op
```

Append to `tests/test_store.py`:

```python
def test_upsert_stores_parent_channel_id():
    conn = connect(":memory:")
    m = replace(msg("t1", channel_id="300", thread_id="300"), parent_channel_id="100")
    upsert_messages(conn, [m], frozenset())
    assert conn.execute("SELECT parent_channel_id FROM messages WHERE id = 't1'").fetchone()[0] == "100"
```

Append to `tests/test_stats.py` (add `from dataclasses import replace` to the imports at the top of the file):

```python
NAMES = {"100": "help", "200": "general", "300": "M1 install thread"}


def _m(id, channel, sentiment, *, minutes=0, thread=None, parent=None):
    m = replace(
        msg(id, f"text {id}", minutes=minutes, channel_id=channel, thread_id=thread),
        channel_name=NAMES[channel], parent_channel_id=parent,
    )
    return m, sentiment


def _channel_db():
    conn = connect(":memory:")
    rows = [
        _m("h1", "100", -2, minutes=0),
        _m("h2", "100", -1, minutes=1),
        _m("t1", "300", -2, minutes=2, thread="300", parent="100"),
        _m("g1", "200", 2, minutes=3),
    ]
    upsert_messages(conn, [m for m, _ in rows], frozenset())
    for m, s in rows:
        set_triage(conn, m.id, sentiment=s)
    return conn


START, END = T0 - timedelta(hours=1), T0 + timedelta(hours=1)


def test_channel_scope_includes_threads_of_the_channel():
    conn = _channel_db()
    help_ = stats.period_summary(conn, START, END, channels=("100",))
    assert help_["messages"] == 3 and help_["negative"] == 3
    assert stats.period_summary(conn, START, END, channels=("200",))["messages"] == 1
    assert stats.period_summary(conn, START, END)["messages"] == 4


def test_series_and_samples_respect_channel_scope():
    conn = _channel_db()
    day = T0.replace(hour=0)
    series = stats.sentiment_series(conn, day, day + timedelta(days=1), channels=("200",))
    assert series == [{"day": "2026-09-28", "messages": 1, "avg_sentiment": 2.0}]
    ids = {m["message_id"] for m in stats.sample_messages(conn, START, END, channels=("100",), limit=10)}
    assert ids == {"h1", "h2", "t1"}


def test_theme_scores_respect_channel_scope():
    conn = _channel_db()
    with conn:
        conn.execute("INSERT INTO themes (id, name, created_at) VALUES (1, 'install', ?)", (to_iso(T0),))
        for mid in ("h1", "t1", "g1"):
            conn.execute("INSERT INTO message_themes (message_id, theme_id) VALUES (?, 1)", (mid,))
    now = T0 + timedelta(hours=1)
    assert stats.theme_scores(conn, now, channels=("100",))[0].volume == 2
    assert stats.theme_scores(conn, now, channels=("200",))[0].volume == 1
    assert stats.theme_scores(conn, now)[0].volume == 3


def test_top_channels_lists_top_level_channels_busiest_first():
    conn = _channel_db()
    assert stats.top_channels(conn) == [
        {"id": "100", "name": "help", "messages": 2},
        {"id": "200", "name": "general", "messages": 1},
    ]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py tests/test_store.py tests/test_stats.py -q`
Expected: FAIL (`parent_channel_id` column missing; `period_summary() got an unexpected keyword argument 'channels'`; `top_channels` not defined).

- [ ] **Step 3: Implement**

In `pulse/db.py`, add the column at the end of the `messages` table in `SCHEMA` (after `source TEXT NOT NULL`):

```sql
    source TEXT NOT NULL,
    parent_channel_id TEXT
```

Replace `_TRIAGE_ADDED_COLUMNS` and `_migrate` with:

```python
# Columns added after a table was first created. CREATE TABLE IF NOT EXISTS leaves an
# existing table untouched, so databases created earlier get them via ALTER TABLE.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "triage": (
        ("needs_reply_p", "REAL"),
        ("kind_confidence", "REAL"),
        ("labeler", "TEXT NOT NULL DEFAULT 'llm'"),
        ("themed_at", "TEXT"),
    ),
    "messages": (("parent_channel_id", "TEXT"),),
}


def _migrate(conn: sqlite3.Connection) -> None:
    with conn:
        for table, columns in _ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
```

In `pulse/store.py`, append `"parent_channel_id"` as the last entry of `_COLUMNS` and `m.parent_channel_id` as the last value returned by `_values` (the two must stay in the same order):

```python
_COLUMNS = (
    "guild_id", "channel_id", "channel_name", "thread_id", "author_id", "author_name",
    "author_avatar_url", "is_team", "is_bot", "content", "created_at", "edited_at",
    "reply_to_id", "source", "parent_channel_id",
)
```

```python
def _values(m: Message, team_ids: frozenset[str]) -> tuple:
    return (
        m.guild_id, m.channel_id, m.channel_name, m.thread_id, m.author_id, m.author_name,
        m.author_avatar_url, int(m.author_id in team_ids), int(m.is_bot), m.content,
        to_iso(m.created_at), to_iso(m.edited_at) if m.edited_at else None,
        m.reply_to_id, m.source, m.parent_channel_id,
    )
```

In `pulse/stats.py`, add after `_like`:

```python
def scope_clause(channels: tuple[str, ...] | None) -> tuple[str, list]:
    """SQL limiting messages `m` to the given top-level channels and their threads.

    Returns ("", []) when unscoped. Rows imported before parent_channel_id existed count
    under their own channel id until they are re-imported.
    """
    if not channels:
        return "", []
    marks = ",".join("?" * len(channels))
    return f" AND (m.channel_id IN ({marks}) OR m.parent_channel_id IN ({marks}))", [*channels, *channels]


def top_channels(conn: sqlite3.Connection) -> list[dict]:
    """Top-level channels (not threads) that have messages, busiest first."""
    rows = conn.execute(
        "SELECT channel_id, MAX(channel_name) AS name, COUNT(*) AS n FROM messages"
        " WHERE thread_id IS NULL GROUP BY channel_id ORDER BY n DESC, channel_id"
    ).fetchall()
    return [{"id": r["channel_id"], "name": r["name"] or r["channel_id"], "messages": r["n"]} for r in rows]
```

Replace `period_summary` and `sentiment_series` with:

```python
def period_summary(
    conn: sqlite3.Connection, start: datetime, end: datetime, *, channels: tuple[str, ...] | None = None
) -> dict:
    scope, scope_params = scope_clause(channels)
    rows = conn.execute(
        "SELECT t.sentiment, t.kind, t.needs_reply FROM messages m JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?{scope}",
        (to_iso(start), to_iso(end), *scope_params),
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
    conn: sqlite3.Connection, start: datetime, end: datetime, *, channels: tuple[str, ...] | None = None
) -> list[dict]:
    scope, scope_params = scope_clause(channels)
    rows = conn.execute(
        "SELECT substr(m.created_at, 1, 10) AS day, COUNT(*) AS n, AVG(t.sentiment) AS avg"
        " FROM messages m JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?{scope} GROUP BY day",
        (to_iso(start), to_iso(end), *scope_params),
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
```

In `theme_scores`, add the keyword `channels: tuple[str, ...] | None = None` after `start` in the signature, and replace the `rows = conn.execute(...)` statement with:

```python
    scope, scope_params = scope_clause(channels)
    rows = conn.execute(
        "SELECT mt.theme_id, m.id AS message_id, m.created_at, t.sentiment, t.kind"
        " FROM message_themes mt JOIN messages m ON m.id = mt.message_id"
        " JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?{scope}",
        (to_iso(prev_start), to_iso(now), *scope_params),
    ).fetchall()
```

In `sample_messages`, add the keyword `channels: tuple[str, ...] | None = None` after `most_negative` in the signature, and right after the two lines that add the `created_at` window (`sql += f" WHERE {_COMMUNITY} ..."` and `params += [to_iso(start), to_iso(end)]`) add:

```python
    scope, scope_params = scope_clause(channels)
    sql += scope
    params += scope_params
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py tests/test_store.py tests/test_stats.py -q`
Expected: PASS. Then run the full suite once: `.venv/bin/pytest -q` (all pass).

- [ ] **Step 5: Commit**

```bash
git add pulse/db.py pulse/store.py pulse/stats.py tests/test_db.py tests/test_store.py tests/test_stats.py
git commit -m "feat: channel scoping for stats, parent channel on messages" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Reply-time stats and channel breakdown

**Files:**
- Modify: `pulse/stats.py`
- Test: `tests/test_stats.py` (append)

**Interfaces:**
- Consumes: `scope_clause`, `top_channels`, `period_summary(channels=)` (Task 1); `from_iso` (models).
- Produces:
  - `REPLY_SLA_HOURS = 24`
  - `first_team_reply(conn, message_id: str, thread_id: str | None, created_at: str) -> str | None` (ISO time of the earliest staff reply, same matching as the mod queue)
  - `reply_stats(conn, start, end, now, *, channels=None) -> dict` keys `needs_reply, answered, median_minutes (float | None), waiting_over_24h`
  - `channel_breakdown(conn, start, end, now) -> list[dict]` keys `id, name, messages, avg_sentiment, negative_share, needs_reply, median_reply_minutes, waiting_over_24h`, busiest first, channels with no messages in the window left out.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stats.py`:

```python
def _reply_db():
    """Questions in #help (100), its thread 300, and #general (200); staff author id is t1."""
    conn = connect(":memory:")
    team = frozenset({"t1"})
    rows = [
        replace(msg("q1", "how do I log in?", minutes=0, channel_id="100"), channel_name="help"),
        replace(msg("s1", "use exchange_token", minutes=30, channel_id="100", author_id="t1",
                    author_name="staff", reply_to_id="q1"), channel_name="help"),
        replace(msg("q2", "wheel error", minutes=10, channel_id="300", thread_id="300"),
                channel_name="M1 thread", parent_channel_id="100"),
        replace(msg("s2", "fixed in 2.0.1", minutes=70, channel_id="300", thread_id="300", author_id="t1",
                    author_name="staff"), channel_name="M1 thread", parent_channel_id="100"),
        replace(msg("q3", "docs 404?", minutes=20, channel_id="100"), channel_name="help"),
        replace(msg("s3", "fixed", minutes=40, channel_id="q3", thread_id="q3", author_id="t1",
                    author_name="staff"), channel_name="docs 404?", parent_channel_id="100"),
        replace(msg("q4", "429s again", minutes=60, channel_id="200"), channel_name="general"),
        replace(msg("q5", "still broken", minutes=20 * 60, channel_id="200"), channel_name="general"),
        replace(msg("s0", "early staff note", minutes=-5, channel_id="200", author_id="t1",
                    author_name="staff", reply_to_id="q4"), channel_name="general"),
        replace(msg("c1", "nice release", minutes=5, channel_id="200"), channel_name="general"),
    ]
    upsert_messages(conn, rows, team)
    for mid in ("q1", "q2", "q3", "q4", "q5"):
        set_triage(conn, mid, sentiment=-1, needs_reply=True)
    set_triage(conn, "c1", sentiment=2, needs_reply=False, kind="praise")
    return conn


REPLY_START, REPLY_NOW = T0 - timedelta(days=1), T0 + timedelta(hours=30)


def test_reply_stats_median_and_waiting():
    conn = _reply_db()
    r = stats.reply_stats(conn, REPLY_START, REPLY_NOW, REPLY_NOW)
    # q1 30 min (reply), q2 60 min (same thread), q3 20 min (thread started from it);
    # q4 unanswered for 29 h (the staff note came before it); q5 unanswered for 10 h.
    assert r == {"needs_reply": 5, "answered": 3, "median_minutes": 30.0, "waiting_over_24h": 1}


def test_reply_stats_with_no_questions():
    conn = connect(":memory:")
    assert stats.reply_stats(conn, REPLY_START, REPLY_NOW, REPLY_NOW) == {
        "needs_reply": 0, "answered": 0, "median_minutes": None, "waiting_over_24h": 0,
    }


def test_reply_stats_respect_channel_scope():
    conn = _reply_db()
    help_ = stats.reply_stats(conn, REPLY_START, REPLY_NOW, REPLY_NOW, channels=("100",))
    assert help_ == {"needs_reply": 3, "answered": 3, "median_minutes": 30.0, "waiting_over_24h": 0}


def test_channel_breakdown_per_top_level_channel():
    conn = _reply_db()
    rows = stats.channel_breakdown(conn, REPLY_START, REPLY_NOW, REPLY_NOW)
    assert [r["id"] for r in rows] == ["100", "200"]
    help_, general = rows
    assert help_ == {
        "id": "100", "name": "help", "messages": 3, "avg_sentiment": -1.0, "negative_share": 1.0,
        "needs_reply": 3, "median_reply_minutes": 30.0, "waiting_over_24h": 0,
    }
    assert general["messages"] == 3 and general["needs_reply"] == 2 and general["waiting_over_24h"] == 1
    assert general["median_reply_minutes"] is None
```

Note: `msg(..., minutes=-5)` places the staff note before q4, so it must not count as an answer. `top_channels` orders channels by total message count (including staff), and `channel_breakdown` re-sorts by community message count, then id.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stats.py -q`
Expected: FAIL with `AttributeError: module 'pulse.stats' has no attribute 'reply_stats'`.

- [ ] **Step 3: Implement**

In `pulse/stats.py`, add `import statistics` to the imports and change the models import to `from pulse.models import KINDS, from_iso, to_iso`. Then add:

```python
REPLY_SLA_HOURS = 24


def first_team_reply(
    conn: sqlite3.Connection, message_id: str, thread_id: str | None, created_at: str
) -> str | None:
    """ISO time of the earliest staff message after this one that replies to it, is in its
    thread, or is in the thread started from it (the mod queue's matching). None if none."""
    row = conn.execute(
        "SELECT MIN(r.created_at) AS first FROM messages r WHERE r.is_team = 1 AND r.created_at > ?"
        " AND (r.reply_to_id = ? OR (? IS NOT NULL AND r.thread_id = ?) OR r.thread_id = ?)",
        (created_at, message_id, thread_id, thread_id, message_id),
    ).fetchone()
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
    for r in rows:
        first = first_team_reply(conn, r["id"], r["thread_id"], r["created_at"])
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


def channel_breakdown(conn: sqlite3.Connection, start: datetime, end: datetime, now: datetime) -> list[dict]:
    """Per top-level channel (threads included): volume, mood and reply times for the window."""
    out = []
    for ch in top_channels(conn):
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stats.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once (all pass).

- [ ] **Step 5: Commit**

```bash
git add pulse/stats.py tests/test_stats.py
git commit -m "feat: reply-time stats and per-channel breakdown" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Pain point status

**Files:**
- Modify: `pulse/db.py` (schema)
- Create: `pulse/theme_status.py`
- Test: `tests/test_theme_status.py`

**Interfaces:**
- Consumes: `stats.theme_resolution`, `stats.theme_member_ids`; `to_iso`, `from_iso`.
- Produces:
  - table `theme_status(theme_id INTEGER PRIMARY KEY REFERENCES themes(id), status TEXT NOT NULL CHECK (status IN ('new','acknowledged','in_progress','shipped')), note TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL, shipped_at TEXT)`
  - `STATUSES = ("new", "acknowledged", "in_progress", "shipped")`, `LABELS: dict[str, str]`, `NOTE_MAX = 500`, `SHIP_WINDOW_DAYS = 14`
  - `set_status(conn, theme_id: int, status: str, note: str, now: datetime) -> None` (writes the row for the theme's active root; `ValueError` for an unknown status; `LookupError` for an unknown theme)
  - `statuses(conn) -> dict[int, dict]` (root theme id → `{status, label, note, updated_at, shipped_at}`; themes never set are absent)
  - `status_for(conn, theme_id: int) -> dict` (same dict; `{"status": "new", "label": "Not triaged", "note": "", "updated_at": None, "shipped_at": None}` when never set)
  - `shipped_comparison(conn, theme_id: int, now: datetime) -> dict | None` keys `shipped_at, days, before, after` (each side `{messages, avg_sentiment}`); `None` unless the status is shipped.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_theme_status.py`:

```python
from datetime import timedelta

import pytest

from pulse import theme_status as ts
from pulse.db import connect
from pulse.models import to_iso
from pulse.store import upsert_messages
from tests.fakes import T0, msg, set_triage


def _db():
    conn = connect(":memory:")
    with conn:
        conn.execute("INSERT INTO themes (id, name, created_at) VALUES (1, 'auth docs', ?)", (to_iso(T0),))
        conn.execute("INSERT INTO themes (id, name, created_at) VALUES (2, 'login', ?)", (to_iso(T0),))
    return conn


def test_set_and_read_status():
    conn = _db()
    ts.set_status(conn, 1, "acknowledged", "  docs team rewriting  ", T0)
    assert ts.status_for(conn, 1) == {
        "status": "acknowledged", "label": "Acknowledged", "note": "docs team rewriting",
        "updated_at": to_iso(T0), "shipped_at": None,
    }
    assert ts.status_for(conn, 2)["status"] == "new"
    assert ts.status_for(conn, 2)["label"] == "Not triaged"
    assert set(ts.statuses(conn)) == {1}


def test_rejects_unknown_status_and_theme():
    conn = _db()
    with pytest.raises(ValueError):
        ts.set_status(conn, 1, "done", "", T0)
    with pytest.raises(LookupError):
        ts.set_status(conn, 99, "acknowledged", "", T0)


def test_note_is_capped():
    conn = _db()
    ts.set_status(conn, 1, "acknowledged", "x" * 900, T0)
    assert len(ts.status_for(conn, 1)["note"]) == ts.NOTE_MAX


def test_status_moves_to_merge_target_newest_wins():
    conn = _db()
    ts.set_status(conn, 2, "in_progress", "fixing login", T0)
    ts.set_status(conn, 1, "acknowledged", "older", T0 - timedelta(hours=1))
    with conn:
        conn.execute("UPDATE themes SET status = 'merged', merged_into = 1 WHERE id = 2")
    assert ts.status_for(conn, 1)["status"] == "in_progress"
    assert ts.status_for(conn, 2)["status"] == "in_progress"


def test_shipped_at_is_kept_while_shipped_and_cleared_after():
    conn = _db()
    ts.set_status(conn, 1, "shipped", "2.0.1", T0)
    ts.set_status(conn, 1, "shipped", "2.0.1, docs updated", T0 + timedelta(hours=2))
    assert ts.status_for(conn, 1)["shipped_at"] == to_iso(T0)
    ts.set_status(conn, 1, "in_progress", "regressed", T0 + timedelta(hours=3))
    assert ts.status_for(conn, 1)["shipped_at"] is None


def test_shipped_comparison_uses_equal_windows():
    conn = _db()
    rows = [msg(f"b{i}", minutes=-60 * (i + 1)) for i in range(3)] + [msg("a0", minutes=60)]
    rows.append(msg("old", minutes=-60 * 24 * 5))  # outside the before window
    upsert_messages(conn, rows, frozenset())
    for m in rows:
        set_triage(conn, m.id, sentiment=-2 if m.id.startswith("b") else 1)
        with conn:
            conn.execute("INSERT INTO message_themes (message_id, theme_id) VALUES (?, 1)", (m.id,))
    assert ts.shipped_comparison(conn, 1, T0) is None
    ts.set_status(conn, 1, "shipped", "", T0)
    now = T0 + timedelta(days=2)
    c = ts.shipped_comparison(conn, 1, now)
    assert c["days"] == 2.0
    assert c["shipped_at"] == to_iso(T0)
    assert c["before"] == {"messages": 3, "avg_sentiment": -2.0}
    assert c["after"] == {"messages": 1, "avg_sentiment": 1.0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_theme_status.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.theme_status'`.

- [ ] **Step 3: Implement**

Add to `SCHEMA` in `pulse/db.py`, after the `themes` table:

```sql
CREATE TABLE IF NOT EXISTS theme_status (
    theme_id INTEGER PRIMARY KEY REFERENCES themes(id),
    status TEXT NOT NULL CHECK (status IN ('new', 'acknowledged', 'in_progress', 'shipped')),
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    shipped_at TEXT
);
```

Create `pulse/theme_status.py`:

```python
"""Pain point status set by the community team (spec 15.2).

A status belongs to the active root theme. A status set on a theme that later merged
moves to the theme it merged into; when several apply, the newest update wins.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from pulse.models import from_iso, to_iso
from pulse.stats import theme_member_ids, theme_resolution

STATUSES = ("new", "acknowledged", "in_progress", "shipped")
LABELS = {
    "new": "Not triaged",
    "acknowledged": "Acknowledged",
    "in_progress": "Fix in progress",
    "shipped": "Fix shipped",
}
NOTE_MAX = 500
SHIP_WINDOW_DAYS = 14

_DEFAULT = {"status": "new", "label": LABELS["new"], "note": "", "updated_at": None, "shipped_at": None}


def statuses(conn: sqlite3.Connection) -> dict[int, dict]:
    resolved = theme_resolution(conn)
    out: dict[int, dict] = {}
    for r in conn.execute("SELECT * FROM theme_status ORDER BY updated_at, theme_id"):
        root = resolved.get(r["theme_id"], r["theme_id"])
        out[root] = {
            "status": r["status"],
            "label": LABELS[r["status"]],
            "note": r["note"],
            "updated_at": r["updated_at"],
            "shipped_at": r["shipped_at"],
        }
    return out


def status_for(conn: sqlite3.Connection, theme_id: int) -> dict:
    root = theme_resolution(conn).get(theme_id, theme_id)
    return statuses(conn).get(root, dict(_DEFAULT))


def set_status(conn: sqlite3.Connection, theme_id: int, status: str, note: str, now: datetime) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    resolved = theme_resolution(conn)
    if theme_id not in resolved:
        raise LookupError(f"unknown theme {theme_id}")
    root = resolved[theme_id]
    current = status_for(conn, root)
    if status == "shipped":
        shipped_at = current["shipped_at"] if current["status"] == "shipped" and current["shipped_at"] else to_iso(now)
    else:
        shipped_at = None
    with conn:
        conn.execute(
            "INSERT INTO theme_status (theme_id, status, note, updated_at, shipped_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(theme_id) DO UPDATE SET status = excluded.status, note = excluded.note,"
            " updated_at = excluded.updated_at, shipped_at = excluded.shipped_at",
            (root, status, note.strip()[:NOTE_MAX], to_iso(now), shipped_at),
        )


def _window(conn: sqlite3.Connection, member_ids: list[int], start: datetime, end: datetime) -> dict:
    marks = ",".join("?" * len(member_ids))
    rows = conn.execute(
        "SELECT DISTINCT m.id, t.sentiment FROM message_themes mt JOIN messages m ON m.id = mt.message_id"
        " JOIN triage t ON t.message_id = m.id"
        f" WHERE mt.theme_id IN ({marks}) AND m.is_team = 0 AND m.is_bot = 0"
        " AND m.created_at >= ? AND m.created_at < ?",
        (*member_ids, to_iso(start), to_iso(end)),
    ).fetchall()
    n = len(rows)
    return {"messages": n, "avg_sentiment": round(sum(r["sentiment"] for r in rows) / n, 3) if n else None}


def shipped_comparison(conn: sqlite3.Connection, theme_id: int, now: datetime) -> dict | None:
    """Volume and mood in equal windows (up to 14 days) before and after the fix shipped."""
    current = status_for(conn, theme_id)
    if current["status"] != "shipped" or not current["shipped_at"]:
        return None
    shipped = from_iso(current["shipped_at"])
    after_end = max(shipped, min(shipped + timedelta(days=SHIP_WINDOW_DAYS), now))
    span = after_end - shipped
    ids = theme_member_ids(conn, theme_id)
    return {
        "shipped_at": current["shipped_at"],
        "days": round(span.total_seconds() / 86400, 1),
        "before": _window(conn, ids, shipped - span, shipped),
        "after": _window(conn, ids, shipped, after_end),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_theme_status.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 5: Commit**

```bash
git add pulse/db.py pulse/theme_status.py tests/test_theme_status.py
git commit -m "feat: pain point status with before/after comparison when shipped" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 4: Web foundation (app factory, filters, message card, layout, CSS)

**Files:**
- Modify: `pyproject.toml`, `pulse/citations.py`
- Create: `pulse/web/__init__.py`, `pulse/web/settings.py`, `pulse/web/filters.py`, `pulse/web/fmt.py`, `pulse/web/cards.py`, `pulse/web/context.py`, `pulse/web/deps.py`, `pulse/web/app.py`, `pulse/web/views/__init__.py`, `pulse/web/views/overview.py`, `pulse/web/templates/base.html`, `pulse/web/templates/_macros.html`, `pulse/web/templates/overview.html`, `pulse/web/static/pulse.css`
- Create: `tests/web_fakes.py`
- Test: `tests/test_web_foundation.py`

**Interfaces:**
- Consumes: `stats.top_channels`, `stats.scope_clause`, `stats.theme_scores(channels=)` (Task 1); `pulse.links.jump_link`; `pulse.db.connect`.
- Produces:
  - `pulse.web.settings.WebSettings(db_path: Path, config: Config, server_name: str = "Discord server", demo: bool = False, clock: Callable[[], datetime] = utc now, llm_factory: Callable[[sqlite3.Connection, Config], LLMClient] | None = None)` with properties `agents_on: bool` and `agents_off_text: str`.
  - `pulse.web.filters`: `WINDOWS = (7, 14, 30, 90)`, `DEFAULT_DAYS = 14`, `Filters(days, channel, now)` with `start`, `end`, `channels`, `qs(**extra) -> str`; `parse_filters(days, channel, now, known_channels) -> Filters`.
  - `pulse.web.cards`: `CARD_SQL`, `card(row) -> dict` (keys `message_id, link, author, author_id, avatar_url, is_team, channel, content, created_at, kind, sentiment`), `cards_by_ids(conn, ids) -> list[dict]` (input order, unknown ids skipped).
  - `pulse.web.fmt`: Jinja filters `age(created_at, now)`, `utc(created_at)`, `signed(value, digits=2)`, `minutes(value)`, `avatar_color(name)`, `kind_label(kind)`; `KIND_LABELS`.
  - `pulse.web.context`: `PATHS`, `spend_today(conn, now) -> float`, `open_queue_count(conn, channels) -> int`, `bug_count(conn, start, end, channels) -> int`, `base_context(request, conn, f, active) -> dict`.
  - `pulse.web.deps`: `get_conn` (yields a per-request connection), `get_filters` (FastAPI dependency reading `days` and `channel`), `render(request, template, conn, f, active, *, status_code=200, **ctx) -> HTMLResponse`.
  - `pulse.web.app.create_app(settings: WebSettings) -> FastAPI` (state: `settings`, `templates`; static files at `/static`).
  - `pulse.citations.render_html(markdown_text: str, conn) -> str`.
  - Templates: `base.html` (blocks `title`, `main`), `_macros.html` (macro `message_card(m, now, f, reason=None)`, accepts a `{% call %}` body for action buttons; macros `status_pill(status, label)` and `range_selector(f, path, extra={})`).
  - `tests/web_fakes.py`: `NOW`, `CONFIG`, `LAUNCH`, `seed(conn)`, `make_client(tmp_path, *, seeded=True, llm_factory=None, demo=False, config=CONFIG) -> TestClient`.

- [ ] **Step 1: Add the dependencies and install them**

In `pyproject.toml`, extend `dependencies` and package data:

```toml
dependencies = [
    "anthropic>=0.40",
    "openai>=1.50",
    "jsonschema>=4.21",
    "httpx>=0.27",
    "fastapi>=0.115",
    "jinja2>=3.1",
    "uvicorn>=0.30",
    "python-multipart>=0.0.9",
    "markdown>=3.6",
]
```

```toml
[tool.setuptools.package-data]
"pulse.agents" = ["prompts/*.md"]
"pulse.web" = ["templates/*.html", "static/*"]
```

Run: `uv pip install --python .venv/bin/python -e '.[dev]'`
Expected: installs fastapi, jinja2, uvicorn, python-multipart, markdown.

- [ ] **Step 2: Create the test helpers**

Create `tests/web_fakes.py`:

```python
"""Web test helpers: a TestClient over a temp database and a small seeded community."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from pulse.config import Launch
from pulse.db import connect
from pulse.models import to_iso
from pulse.modqueue import refresh_mod_queue
from pulse.store import sync_launches, upsert_messages
from pulse.web.app import create_app
from pulse.web.settings import WebSettings
from tests.fakes import T0, make_config, msg, set_triage

NOW = T0 + timedelta(days=2)  # 2026-09-30 12:00 UTC
LAUNCH = Launch("v2.0", "2026-09-28", ("install", "v2"))
CONFIG = make_config(launches=(LAUNCH,))
NAMES = {"100": "help", "200": "general", "300": "M1 install thread"}


def _m(id, content, channel, *, minutes, author="u1", name="alice", thread=None, parent=None, reply_to=None):
    m = msg(id, content, minutes=minutes, channel_id=channel, thread_id=thread,
            author_id=author, author_name=name, reply_to_id=reply_to)
    return replace(m, channel_name=NAMES[channel], parent_channel_id=parent)


def seed(conn) -> None:
    """#help (100) with thread 300, #general (200); staff author t1 ('sam'); two themes;
    a launch on 2026-09-28; one weekly digest; two agent runs; mod queue refreshed at NOW
    (q1 and g2 frustrated, q3 unanswered; q2 was answered by staff)."""
    rows = [
        _m("q1", "Install fails on M1 <script>alert(1)</script>", "100", minutes=0),
        _m("q2", "Where is exchange_token in the auth docs?", "100", minutes=60, author="u2", name="bob"),
        _m("s1", "It moved to auth.exchange()", "100", minutes=90, author="t1", name="sam", reply_to="q2"),
        _m("q3", "arm64 wheel still missing on 2.0.1", "300", minutes=120, author="u3", name="carol",
           thread="300", parent="100"),
        _m("p1", "honestly v2 is the best release yet", "200", minutes=180, author="u4", name="uma"),
        _m("g1", "anyone at the Berlin meetup?", "200", minutes=200, author="u5", name="dan"),
        _m("g2", "status page is green but builds fail", "200", minutes=240, author="u6", name="erin"),
    ]
    upsert_messages(conn, rows, CONFIG.team_member_ids)
    labels = {
        "q1": dict(sentiment=-2, needs_reply=True, kind="bug", topics=("m1 install",)),
        "q2": dict(sentiment=-1, needs_reply=True, kind="docs", topics=("auth docs",)),
        "q3": dict(sentiment=-1, needs_reply=True, kind="bug", topics=("arm64 wheel",)),
        "p1": dict(sentiment=2, needs_reply=False, kind="praise", topics=("v2",)),
        "g1": dict(sentiment=0, needs_reply=False, kind="other"),
        "g2": dict(sentiment=-2, needs_reply=True, kind="bug", topics=("builds",)),
    }
    for mid, kw in labels.items():
        set_triage(conn, mid, **kw)
    with conn:
        conn.execute(
            "INSERT INTO themes (id, name, description, created_at) VALUES"
            " (1, 'Install fails on M1', 'arm64 wheels missing', ?), (2, 'Auth docs', 'token exchange step', ?)",
            (to_iso(T0), to_iso(T0)),
        )
        for mid, tid in (("q1", 1), ("q3", 1), ("q2", 2)):
            conn.execute("INSERT INTO message_themes (message_id, theme_id) VALUES (?, ?)", (mid, tid))
        conn.execute(
            "INSERT INTO digests (kind, period_start, period_end, markdown, cited_message_ids, created_at)"
            " VALUES ('weekly', ?, ?, ?, ?, ?)",
            (to_iso(NOW - timedelta(days=7)), to_iso(NOW),
             "## What's landing well\nPraise from [[msg:p1]].\n\n## Top pain points\nM1 installs [[msg:q1]].",
             json.dumps(["p1", "q1"]), to_iso(NOW - timedelta(hours=1))),
        )
        conn.execute(
            "INSERT INTO agent_runs (agent, model, input_tokens, output_tokens, cost_usd, status, started_at,"
            " finished_at) VALUES ('triage', 'anthropic:m-triage', 1000, 200, 0.04, 'ok', ?, ?)",
            (to_iso(NOW - timedelta(hours=2)), to_iso(NOW - timedelta(hours=2))),
        )
        conn.execute(
            "INSERT INTO agent_runs (agent, model, cost_usd, status, error, started_at, finished_at)"
            " VALUES ('digest', 'anthropic:m-digest', 0.0, 'failed', 'provider 500', ?, ?)",
            (to_iso(NOW - timedelta(hours=3)), to_iso(NOW - timedelta(hours=3))),
        )
    sync_launches(conn, CONFIG.launches)
    refresh_mod_queue(conn, CONFIG, NOW)


def make_client(tmp_path: Path, *, seeded=True, llm_factory=None, demo=False, config=CONFIG) -> TestClient:
    path = tmp_path / "pulse.db"
    conn = connect(path)
    if seeded:
        seed(conn)
    conn.close()
    settings = WebSettings(
        db_path=path, config=config, server_name="Acme SDK Community", demo=demo,
        clock=lambda: NOW, llm_factory=llm_factory,
    )
    return TestClient(create_app(settings))
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_web_foundation.py`:

```python
from datetime import timedelta

from pulse.citations import render_html
from pulse.db import connect
from pulse.web.cards import cards_by_ids
from pulse.web.filters import parse_filters
from tests.fakes import make_config
from tests.web_fakes import NOW, make_client, seed


def test_static_css_is_served(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/static/pulse.css")
    assert r.status_code == 200
    assert "--accent" in r.text and "nav a{" in r.text


def test_parse_filters_falls_back():
    f = parse_filters("abc", "999", NOW, {"100"})
    assert (f.days, f.channel, f.channels) == (14, None, None)
    assert parse_filters("5", None, NOW, set()).days == 14
    f = parse_filters("30", "100", NOW, {"100"})
    assert (f.days, f.channel, f.channels) == (30, "100", ("100",))
    assert f.start == NOW - timedelta(days=30) and f.end == NOW


def test_filters_qs_keeps_channel_and_applies_overrides():
    f = parse_filters("7", "100", NOW, {"100"})
    assert f.qs() == "?days=7&channel=100"
    assert f.qs(days=30, theme=2) == "?days=30&channel=100&theme=2"
    assert parse_filters(None, None, NOW, set()).qs(author_id="") == "?days=14"


def test_render_html_escapes_raw_html_and_links_citations(tmp_path):
    conn = connect(tmp_path / "x.db")
    seed(conn)
    html = render_html("Hi <img src=x onerror=alert(1)> [[msg:q1]] and [[msg:nope]]\n\n- item", conn)
    assert "<img" not in html and "&lt;img" in html
    assert 'href="https://discord.com/channels/900/100/q1"' in html and "@alice" in html
    assert "[missing message]" in html
    assert "<li>item</li>" in html


def _macro(client):
    return client.app.state.templates.env.get_template("_macros.html").module.message_card


def test_message_card_escapes_content_and_links(tmp_path):
    client = make_client(tmp_path)
    card = cards_by_ids(connect(tmp_path / "pulse.db"), ["q1"])[0]
    html = str(_macro(client)(card, NOW, parse_filters(None, None, NOW, set())))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "https://discord.com/channels/900/100/q1" in html
    assert "/messages?days=14&amp;author_id=u1" in html
    assert "All from alice" in html and "2d ago" in html and "#help" in html


def test_message_card_team_pill_and_long_text(tmp_path):
    client = make_client(tmp_path)
    card = cards_by_ids(connect(tmp_path / "pulse.db"), ["s1"])[0]
    f = parse_filters(None, None, NOW, set())
    html = str(_macro(client)(card, NOW, f))
    assert 'class="pill team"' in html
    long = str(_macro(client)({**card, "content": "x" * 450}, NOW, f))
    assert "Show all" in long


def test_cards_by_ids_keeps_order_and_skips_unknown(tmp_path):
    conn = connect(tmp_path / "x.db")
    seed(conn)
    assert [c["message_id"] for c in cards_by_ids(conn, ["g2", "nope", "q1", "g2"])] == ["g2", "q1"]


def test_layout_renders_on_empty_db_with_bad_params(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/?days=abc&channel=zzz")
    assert r.status_code == 200
    assert "Community pulse" in r.text and "Acme SDK Community" in r.text
    assert 'aria-current="page"' in r.text


def test_layout_shows_budget_banner_when_cap_reached(tmp_path):
    r = make_client(tmp_path, config=make_config(daily_usd_cap=0.01)).get("/")
    assert "reached the $0.01 daily cap" in r.text


def test_nav_counts_follow_channel_filter(tmp_path):
    client = make_client(tmp_path)
    assert '<span class="count hot">3</span>' in client.get("/").text
    assert '<span class="count hot">1</span>' in client.get("/?channel=200").text
```

Note: the seeded queue holds q1 (#help), q3 (#help thread) and g2 (#general), so the Mod queue nav count is 3 for all channels and 1 for `channel=200`.

- [ ] **Step 4: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_foundation.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.web'`.

- [ ] **Step 5: Implement the Python modules**

Add to `pulse/citations.py` (add `import html`, `import logging` and `import markdown` at the top):

```python
log = logging.getLogger(__name__)


def render_html(markdown_text: str, conn: sqlite3.Connection) -> str:
    """HTML for agent reports. Raw HTML from the model is escaped first; each [[msg:id]]
    becomes "@author" linked to the message in Discord (excerpt on hover); unknown ids
    render as "[missing message]" and are logged."""

    def replace(match: re.Match) -> str:
        mid = match.group(1)
        row = conn.execute(
            "SELECT guild_id, channel_id, author_name, content FROM messages WHERE id = ?", (mid,)
        ).fetchone()
        if row is None:
            log.warning("citation to unknown message %s", mid)
            return "[missing message]"
        link = jump_link(row["guild_id"], row["channel_id"], mid)
        excerpt = row["content"][:140]
        return (
            f'<a class="cite" href="{html.escape(link)}" title="{html.escape(excerpt)}"'
            f' target="_blank" rel="noopener">@{html.escape(row["author_name"])}</a>'
        )

    escaped = html.escape(markdown_text, quote=False)
    return markdown.markdown(CITATION_RE.sub(replace, escaped), extensions=["sane_lists"])
```

(`sqlite3` and `jump_link` are already imported in `citations.py`; if `sqlite3` is not, add `import sqlite3`.)

Create `pulse/web/__init__.py` (empty) and `pulse/web/views/__init__.py` (empty).

Create `pulse/web/settings.py`:

```python
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pulse.agents.llm import LLMClient
from pulse.config import Config


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WebSettings:
    db_path: Path
    config: Config
    server_name: str = "Discord server"
    demo: bool = False
    clock: Callable[[], datetime] = field(default=_utc_now)
    llm_factory: Callable[[sqlite3.Connection, Config], LLMClient] | None = None

    @property
    def agents_on(self) -> bool:
        return self.llm_factory is not None and not self.demo

    @property
    def agents_off_text(self) -> str:
        return "Agents are off in demo mode" if self.demo else "Agents are off"
```

Create `pulse/web/filters.py`:

```python
"""The window and channel filter shared by every view (spec 15.3)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

WINDOWS = (7, 14, 30, 90)
DEFAULT_DAYS = 14


@dataclass(frozen=True)
class Filters:
    days: int
    channel: str | None
    now: datetime

    @property
    def start(self) -> datetime:
        return self.now - timedelta(days=self.days)

    @property
    def end(self) -> datetime:
        return self.now

    @property
    def channels(self) -> tuple[str, ...] | None:
        return (self.channel,) if self.channel else None

    def qs(self, **extra) -> str:
        """Query string keeping the filters; extra keys are added or override, empty ones are dropped."""
        params: dict = {"days": self.days}
        if self.channel:
            params["channel"] = self.channel
        params.update(extra)
        return "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})


def parse_filters(days: str | None, channel: str | None, now: datetime, known_channels: set[str]) -> Filters:
    try:
        d = int(days) if days else DEFAULT_DAYS
    except ValueError:
        d = DEFAULT_DAYS
    if d not in WINDOWS:
        d = DEFAULT_DAYS
    return Filters(d, channel if channel and channel in known_channels else None, now)
```

Create `pulse/web/fmt.py`:

```python
"""Jinja filters for the dashboard."""
from __future__ import annotations

from datetime import datetime

from pulse.models import from_iso

KIND_LABELS = {
    "bug": "bug", "question": "question", "feature_request": "feature request",
    "docs": "docs", "praise": "praise", "other": "other",
}
_PALETTE = ("#0E7C86", "#7C3AED", "#B45309", "#2563EB", "#BE185D", "#15803D", "#9333EA", "#C2410C", "#0369A1", "#4D7C0F")


def age(created_at: str, now: datetime) -> str:
    hours = (now - from_iso(created_at)).total_seconds() / 3600
    if hours < 1:
        return f"{max(1, round(hours * 60))} min ago"
    if hours < 48:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24)}d ago"


def utc(created_at: str) -> str:
    dt = from_iso(created_at)
    return f"{dt:%b} {dt.day}, {dt:%H:%M} UTC"


def signed(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "0" if digits == 0 else f"{0:.{digits}f}"
    return f"{value:+.{digits}f}"


def minutes(value) -> str:
    if value is None:
        return "—"
    if value < 60:
        return f"{round(value)} min"
    if value < 48 * 60:
        return f"{value / 60:.1f} h"
    return f"{value / 1440:.1f} d"


def avatar_color(name: str) -> str:
    return _PALETTE[sum(map(ord, name)) % len(_PALETTE)]


def kind_label(kind: str | None) -> str:
    return KIND_LABELS.get(kind or "", kind or "")
```

Create `pulse/web/cards.py`:

```python
"""Rows for the shared message card (spec 5)."""
from __future__ import annotations

import sqlite3
from typing import Iterable

from pulse.links import jump_link

CARD_SQL = (
    "SELECT m.id AS message_id, m.guild_id, m.channel_id, m.channel_name, m.author_id, m.author_name,"
    " m.author_avatar_url, m.is_team, m.content, m.created_at, t.kind, t.sentiment"
    " FROM messages m LEFT JOIN triage t ON t.message_id = m.id"
)


def card(row: sqlite3.Row) -> dict:
    return {
        "message_id": row["message_id"],
        "link": jump_link(row["guild_id"], row["channel_id"], row["message_id"]),
        "author": row["author_name"],
        "author_id": row["author_id"],
        "avatar_url": row["author_avatar_url"],
        "is_team": bool(row["is_team"]),
        "channel": row["channel_name"] or row["channel_id"],
        "content": row["content"],
        "created_at": row["created_at"],
        "kind": row["kind"],
        "sentiment": row["sentiment"],
    }


def cards_by_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> list[dict]:
    """Cards in the order given; duplicates and unknown ids are skipped."""
    ordered = list(dict.fromkeys(ids))
    if not ordered:
        return []
    marks = ",".join("?" * len(ordered))
    rows = {r["message_id"]: r for r in conn.execute(f"{CARD_SQL} WHERE m.id IN ({marks})", ordered)}
    return [card(rows[i]) for i in ordered if i in rows]
```

Create `pulse/web/context.py`:

```python
"""Template context shared by every page: filters, nav counts, channels, budget banner."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pulse import stats
from pulse.models import to_iso
from pulse.web.filters import Filters

PATHS = {
    "overview": "/", "pain": "/pain", "bugs": "/bugs", "queue": "/queue",
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
    }
```

Create `pulse/web/deps.py`:

```python
"""Per-request database connection, filters, and page rendering."""
from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from pulse.db import connect
from pulse.stats import top_channels
from pulse.web.context import base_context
from pulse.web.filters import Filters, parse_filters


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = connect(request.app.state.settings.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_filters(
    request: Request,
    conn: sqlite3.Connection = Depends(get_conn),
    days: str | None = None,
    channel: str | None = None,
) -> Filters:
    known = {c["id"] for c in top_channels(conn)}
    return parse_filters(days, channel, request.app.state.settings.clock(), known)


def render(request: Request, template: str, conn, f: Filters, active: str, *, status_code: int = 200, **ctx) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request, template, {**base_context(request, conn, f, active), **ctx}, status_code=status_code
    )
```

Create `pulse/web/views/overview.py` (Task 5 replaces its body):

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    return render(request, "overview.html", conn, f, "overview")
```

Create `pulse/web/app.py`:

```python
"""FastAPI app factory for the dashboard."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pulse.web import fmt
from pulse.web.settings import WebSettings
from pulse.web.views import overview

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: WebSettings) -> FastAPI:
    app = FastAPI(title="Discord Pulse", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        age=fmt.age, utc=fmt.utc, signed=fmt.signed, minutes=fmt.minutes,
        avatar_color=fmt.avatar_color, kind_label=fmt.kind_label,
    )
    app.state.templates = templates
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    for module in (overview,):
        app.include_router(module.router)
    return app
```

(Later tasks add their view module to the tuple in `create_app` and to the import line.)

- [ ] **Step 6: Create the CSS from the mockup**

Run this once to copy the mockup's stylesheet:

```bash
mkdir -p pulse/web/static pulse/web/templates
.venv/bin/python - <<'EOF'
src = open("docs/design/dashboard-mockup.html", encoding="utf-8").read()
css = src[src.index("<style>") + len("<style>"):src.index("</style>")]
open("pulse/web/static/pulse.css", "w", encoding="utf-8").write(css.strip() + "\n")
EOF
```

Then append these rules to the end of `pulse/web/static/pulse.css`:

```css
/* ---- live dashboard additions (links instead of the mockup's buttons) ---- */
body{margin:0}
a.brand{color:var(--ink);text-decoration:none}
nav a{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:7px 10px;border-radius:7px;color:var(--muted);font-weight:500;text-decoration:none}
nav a:hover{background:var(--sunk);color:var(--ink)}
nav a[aria-current="page"]{background:var(--surface);color:var(--ink);box-shadow:0 0 0 1px var(--line)}
nav a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media (max-width:860px){nav a{border:1px solid var(--line);background:var(--surface)}}
.range a{padding:5px 10px;border-radius:6px;font:500 12px/1 var(--body);color:var(--muted);text-decoration:none}
.range a[aria-current="true"]{background:var(--surface);color:var(--ink);box-shadow:0 1px 2px rgb(0 0 0 / .08)}
.chip{text-decoration:none}
.chip[aria-current="true"]{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
img.av{object-fit:cover}
details.more summary{cursor:pointer;color:var(--accent);font-size:12px;margin:-4px 0 8px}
.banner{margin:0;padding:10px 14px;border-radius:8px;background:var(--warn-soft);color:var(--warn);font-weight:500}
.banner.err{background:var(--neg-soft);color:var(--neg)}
.cite{color:var(--accent);font-weight:500;text-decoration:none;white-space:nowrap}
.cite:hover{text-decoration:underline}
.report{line-height:1.6;max-width:75ch}
.report h2{font:600 15px/1.3 var(--display);margin:18px 0 6px}
.report p,.report ul,.report ol{margin:0 0 10px}
.chart .end-t{fill:var(--ink);font-weight:500}
input[type=text],input[type=search],textarea{font:inherit;color:var(--ink);background:var(--surface);border:1px solid var(--line);border-radius:7px;padding:7px 9px}
textarea{width:100%;min-height:64px;resize:vertical}
.form-row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.btn[disabled]{opacity:.5;cursor:not-allowed}
.pager{display:flex;gap:8px;align-items:center;justify-content:flex-end;margin-top:12px}
.compare{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.spin{color:var(--muted)}
```

- [ ] **Step 7: Create the templates**

Create `pulse/web/templates/base.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>{% block title %}Overview{% endblock %} · Discord Pulse</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:wght@500;600;700&family=IBM+Plex+Sans:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap">
<link rel="stylesheet" href="/static/pulse.css">
<script src="https://unpkg.com/htmx.org@2.0.4" defer></script>
</head>
<body>
<div class="app">
  <aside class="side">
    <a class="brand" href="/{{ f.qs() }}">
      <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true"><rect width="26" height="26" rx="7" fill="var(--accent)"/><path d="M4 14h4l2.5-6 4 11 2.5-5H22" fill="none" stroke="var(--accent-ink)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
      Discord Pulse
    </a>
    <div class="server"><b>{{ server_name }}</b>{% if demo %}Demo data · agents off{% endif %}</div>
    <form class="filter" method="get">
      <label for="ch-sel" class="label">Channel</label>
      <select id="ch-sel" name="channel" onchange="this.form.submit()">
        <option value="">All channels</option>
        {% for c in channels %}<option value="{{ c.id }}"{% if f.channel == c.id %} selected{% endif %}>#{{ c.name }}</option>{% endfor %}
      </select>
      <input type="hidden" name="days" value="{{ f.days }}">
      <noscript><button class="btn" type="submit">Apply</button></noscript>
    </form>
    <nav aria-label="Views">
      <ul>
        {% for key, label, count, hot in nav %}
        <li><a href="{{ paths[key] }}{{ f.qs() }}"{% if active == key %} aria-current="page"{% endif %}>{{ label }}{% if count is not none %} <span class="count{% if hot %} hot{% endif %}">{{ count }}</span>{% endif %}</a></li>
        {% endfor %}
      </ul>
    </nav>
  </aside>
  <main>
    {% if over_cap %}<p class="banner" role="status">Today's spend ${{ "%.2f"|format(spent) }} reached the ${{ "%.2f"|format(cap) }} daily cap. Agents stop until midnight UTC; the dashboard still works.</p>{% endif %}
    {% block main %}{% endblock %}
  </main>
</div>
</body>
</html>
```

Create `pulse/web/templates/_macros.html`:

```html
{% macro message_card(m, now, f, reason=None) -%}
<article class="msg" id="msg-{{ m.message_id }}">
  {% if m.avatar_url %}<img class="av" src="{{ m.avatar_url }}" alt="" loading="lazy">{% else %}<div class="av" style="background:{{ m.author|avatar_color }}" aria-hidden="true">{{ m.author[:1]|upper }}</div>{% endif %}
  <div>
    <div class="mhead">
      <span class="who">{{ m.author }}</span>
      {% if m.is_team %}<span class="pill team">team</span>{% endif %}
      <span class="where">#{{ m.channel }}</span>
      <span title="{{ m.created_at|utc }}">{{ m.created_at|age(now) }}</span>
      {% if reason %}<span class="pill {{ reason }}"><span class="dotc"></span>{{ reason }}</span>{% endif %}
    </div>
    {% if m.content|length > 400 %}
    <p class="mtext">{{ m.content[:400] }}…</p>
    <details class="more"><summary>Show all</summary><p class="mtext">{{ m.content }}</p></details>
    {% else %}
    <p class="mtext">{{ m.content }}</p>
    {% endif %}
    <div class="mfoot">
      <a href="{{ m.link }}" target="_blank" rel="noopener">Open in Discord ↗</a>
      <a href="/messages{{ f.qs(author_id=m.author_id) }}">All from {{ m.author }}</a>
      {% if m.kind %}<span class="pill kind">{{ m.kind|kind_label }}</span>{% endif %}
      {% if m.sentiment is not none %}<span class="sent {{ 'neg' if m.sentiment < 0 else 'pos' if m.sentiment > 0 else 'mut' }}">sentiment {{ m.sentiment|signed(0) }}</span>{% endif %}
      {% if caller is defined %}{{ caller() }}{% endif %}
    </div>
  </div>
</article>
{%- endmacro %}

{% macro status_pill(status, label) -%}
<span class="pill {{ {'new': 'kind', 'acknowledged': 'unanswered', 'in_progress': 'team', 'shipped': 'ok'}[status] }}">{{ label }}</span>
{%- endmacro %}

{% macro range_selector(f, path, extra={}) -%}
<div class="range" role="group" aria-label="Window">
  {% for d in (7, 14, 30, 90) %}<a href="{{ path }}{{ f.qs(days=d, **extra) }}"{% if f.days == d %} aria-current="true"{% endif %}>{{ d }}d</a>{% endfor %}
</div>
{%- endmacro %}
```

Create `pulse/web/templates/overview.html` (Task 5 replaces it):

```html
{% extends "base.html" %}
{% from "_macros.html" import range_selector %}
{% block title %}Overview{% endblock %}
{% block main %}
<div class="head">
  <div><h1>Community pulse</h1><p class="sub">Last {{ f.days }} days</p></div>
  {{ range_selector(f, "/") }}
</div>
{% endblock %}
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_foundation.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once (all pass).

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml pulse/citations.py pulse/web tests/web_fakes.py tests/test_web_foundation.py
git commit -m "feat: dashboard foundation (app factory, filters, message card, layout, mockup CSS)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 5: Overview (charts, stats strip, where it's coming from, attention, praise, digest)

**Files:**
- Modify: `pulse/modqueue.py`, `pulse/web/views/overview.py`, `pulse/web/templates/overview.html`, `pulse/web/static/pulse.css`
- Create: `pulse/web/charts.py`, `pulse/web/queries.py`
- Test: `tests/test_modqueue.py` (append), `tests/test_charts.py`, `tests/test_web_overview.py`

**Interfaces:**
- Consumes: Tasks 1-4 (`stats.*(channels=)`, `reply_stats`, `channel_breakdown`, `theme_status.statuses`/`LABELS`, `cards_by_ids`, `render`, `render_html`).
- Produces:
  - `modqueue.list_open(conn, limit=20, *, channels=None)`; rows gain `queue_id` and `thread_id`.
  - `pulse.web.charts.sentiment_chart(series: list[dict], markers: list[tuple[str, str]]) -> Markup` and `sparkline(values: list[int]) -> Markup`.
  - `pulse.web.queries`: `KIND_COLORS`, `day_keys(start, end) -> list[str]`, `theme_daily(conn, theme_id, start, end, channels) -> list[int]`, `theme_rows(conn, f, limit=20) -> list[dict]` (keys `id, name, description, volume, prev_volume, negativity, trend, trend_text, score, kinds, status, status_label, note, spark`), `queue_cards(conn, f, *, limit=200, reason=None) -> list[dict]` (card keys plus `reason, queue_id`), `launch_markers(conn, start, end) -> list[tuple[str, str]]`, `kind_mix(by_kind) -> list[dict]`, `latest_digest(conn)`, `pct_change(current, previous) -> int | None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_modqueue.py`:

```python
def test_list_open_filters_by_channel_and_exposes_queue_id():
    from dataclasses import replace as _replace

    conn = connect(":memory:")
    rows = [
        _replace(msg("a", "broken", minutes=0, channel_id="100"), channel_name="help"),
        _replace(msg("b", "broken too", minutes=1, channel_id="300", thread_id="300"),
                 channel_name="thread", parent_channel_id="100"),
        _replace(msg("c", "also broken", minutes=2, channel_id="200"), channel_name="general"),
    ]
    upsert_messages(conn, rows, frozenset())
    for m in rows:
        set_triage(conn, m.id, sentiment=-2, needs_reply=False, kind="bug")
    refresh_mod_queue(conn, make_config(), T0 + timedelta(hours=1))
    assert {r["message_id"] for r in list_open(conn, channels=("100",))} == {"a", "b"}
    assert [r["message_id"] for r in list_open(conn, channels=("200",))] == ["c"]
    assert all(isinstance(r["queue_id"], int) for r in list_open(conn))
```

Create `tests/test_charts.py`:

```python
from pulse.web.charts import sentiment_chart, sparkline


def _series(*points):
    return [{"day": f"2026-09-{d:02d}", "messages": n, "avg_sentiment": s} for d, n, s in points]


def test_chart_empty_window_shows_message():
    html = str(sentiment_chart(_series((28, 0, None), (29, 0, None)), []))
    assert "No messages in this window" in html and "<svg" not in html


def test_chart_draws_bars_line_and_launch_marker():
    html = str(sentiment_chart(_series((27, 10, 0.4), (28, 60, -0.2), (29, 50, -0.5)), [("2026-09-28", "v2.0")]))
    assert html.startswith("<svg") and html.count('class="bar"') == 3
    assert 'class="line"' in html and "v2.0" in html and "-0.50" in html


def test_chart_skips_marker_outside_window_and_escapes_label():
    html = str(sentiment_chart(_series((27, 3, 0.1)), [("2026-08-01", "old"), ("2026-09-27", "<b>x</b>")]))
    assert "old" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_chart_clamps_sentiment_to_axis():
    html = str(sentiment_chart(_series((27, 3, -2.0), (28, 3, -1.0)), []))
    # both points sit on the -1 gridline (y = 16 + 204 = 220.0)
    assert 'cy="220.0"' in html


def test_sparkline_handles_zeros():
    html = str(sparkline([0, 0, 0]))
    assert html.startswith('<svg class="spark"') and 'class="l"' in html
```

Create `tests/test_web_overview.py`:

```python
from tests.web_fakes import make_client


def test_overview_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/")
    assert r.status_code == 200
    assert "No messages in this window" in r.text
    assert "No pain points for this filter yet" in r.text
    assert "queue is clear" in r.text
    assert "Nothing is waiting for a reply" in r.text


def test_overview_shows_stats_chart_pain_points_and_cards(tmp_path):
    html = make_client(tmp_path).get("/").text
    assert ">6</span>" in html  # messages in the last 7 days
    assert "30 min" in html and "1 of 4 answered · 3 waiting &gt;24h" in html
    assert "<svg class=\"chart\"" in html and "v2.0" in html
    assert "Install fails on M1" in html and "/pain?days=14&amp;theme=1" in html
    assert "https://discord.com/channels/900/200/g2" in html  # needs attention card
    assert "uma" in html and "honestly v2 is the best release yet" in html  # landing well
    assert 'class="cite"' in html and "@uma" in html  # latest digest excerpt


def test_overview_channel_filter_recomputes(tmp_path):
    html = make_client(tmp_path).get("/?channel=200").text
    assert "#general" in html and "/?days=14&amp;channel=100" not in html
    assert "No pain points for this filter yet" in html
    assert 'id="msg-q1"' not in html and 'id="msg-g2"' in html  # only #general cards
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_modqueue.py tests/test_charts.py tests/test_web_overview.py -q`
Expected: FAIL (`list_open() got an unexpected keyword argument 'channels'`; `No module named 'pulse.web.charts'`).

- [ ] **Step 3: Implement**

In `pulse/modqueue.py`, add `from pulse.stats import scope_clause` to the imports and replace `list_open` with:

```python
def list_open(
    conn: sqlite3.Connection, limit: int = 20, *, channels: tuple[str, ...] | None = None
) -> list[sqlite3.Row]:
    """Open items, highest priority first: frustrated, then Jev's needs_reply
    probability (an LLM-only row counts as its 0/1 label), then oldest."""
    scope, params = scope_clause(channels)
    return conn.execute(
        "SELECT q.id AS queue_id, q.reason, m.id AS message_id, m.guild_id, m.channel_id, m.channel_name,"
        " m.thread_id, m.author_name, m.content, m.created_at, t.needs_reply_p"
        " FROM mod_queue q JOIN messages m ON m.id = q.message_id"
        " LEFT JOIN triage t ON t.message_id = m.id"
        f" WHERE q.status = 'open'{scope}"
        " ORDER BY (q.reason = 'frustrated') DESC, COALESCE(t.needs_reply_p, t.needs_reply, 0) DESC,"
        " m.created_at ASC, m.id ASC"
        " LIMIT ?",
        (*params, limit),
    ).fetchall()
```

Create `pulse/web/charts.py`:

```python
"""Server-side SVG charts in the mockup's style (spec 15.5)."""
from __future__ import annotations

from datetime import date
from html import escape

from markupsafe import Markup

_W, _H, _L, _R, _T, _B = 640, 250, 44, 14, 16, 30


def _nice(value: int) -> int:
    for top in (10, 20, 40, 50, 80, 100, 200, 400, 500, 800, 1000, 2000, 5000, 10000):
        if value <= top:
            return top
    return -(-value // 10000) * 10000


def _short_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}"


def sentiment_chart(series: list[dict], markers: list[tuple[str, str]]) -> Markup:
    """Daily average sentiment (line, clamped to -1..+1) over message volume (bars), with
    vertical markers at the start of each marker day that falls inside the series."""
    if not any(p["messages"] for p in series):
        return Markup('<p class="empty">No messages in this window.</p>')
    n = len(series)
    iw, ih = _W - _L - _R, _H - _T - _B

    def x(i: int) -> float:
        return _L + iw * (i + 0.5) / n

    def ys(v: float) -> float:
        return _T + ih * (1 - (max(-1.0, min(1.0, v)) + 1) / 2)

    top = _nice(max(p["messages"] for p in series))

    def yn(c: float) -> float:
        return _T + ih * (1 - c / top)

    parts: list[str] = []
    for v in (1.0, 0.5, 0.0, -0.5, -1.0):
        y = ys(v)
        parts.append(f'<line class="{"zero" if v == 0 else "grid"}" x1="{_L}" x2="{_W - _R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        label = "0" if v == 0 else f"{v:+g}"
        parts.append(f'<text x="{_L - 8}" y="{y + 4:.1f}" text-anchor="end">{label}</text>')
    for c in (top // 2, top):
        parts.append(f'<text x="{_W - _R}" y="{yn(c) - 4:.1f}" text-anchor="end">{c} msgs</text>')
    bw = iw / n * 0.56
    for i, p in enumerate(series):
        if p["messages"]:
            y = yn(p["messages"])
            parts.append(
                f'<rect class="bar" x="{x(i) - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{_T + ih - y:.1f}" rx="2">'
                f'<title>{p["day"]}: {p["messages"]} messages</title></rect>'
            )
    step = max(1, round(n / 7))
    for i, p in enumerate(series):
        if (n - 1 - i) % step == 0:
            parts.append(f'<text x="{x(i):.1f}" y="{_H - 10}" text-anchor="middle">{_short_day(p["day"])}</text>')
    days = [p["day"] for p in series]
    for day, label in markers:
        if day in days:
            lx = x(days.index(day)) - iw / n * 0.5
            parts.append(f'<line class="launch" x1="{lx:.1f}" x2="{lx:.1f}" y1="{_T}" y2="{_T + ih}"/>')
            parts.append(f'<text class="launch-t" x="{lx - 6:.1f}" y="{_T + ih - 8}" text-anchor="end">{escape(label)}</text>')
    points = [(x(i), ys(p["avg_sentiment"]), p["avg_sentiment"]) for i, p in enumerate(series) if p["avg_sentiment"] is not None]
    if points:
        d = " ".join(f'{"M" if j == 0 else "L"}{px:.1f} {py:.1f}' for j, (px, py, _) in enumerate(points))
        parts.append(f'<path class="area" d="{d} L{points[-1][0]:.1f} {ys(-1):.1f} L{points[0][0]:.1f} {ys(-1):.1f} Z"/>')
        parts.append(f'<path class="line" d="{d}"/>')
        for px, py, _ in points[:-1]:
            parts.append(f'<circle class="dot" cx="{px:.1f}" cy="{py:.1f}" r="2.4"/>')
        px, py, last = points[-1]
        parts.append(f'<circle class="end" cx="{px:.1f}" cy="{py:.1f}" r="4.5"/>')
        parts.append(f'<text class="end-t" x="{px - 8:.1f}" y="{py + 18:.1f}" text-anchor="end">{last:+.2f}</text>')
    return Markup(
        f'<svg class="chart" viewBox="0 0 {_W} {_H}" role="img" aria-label="Daily average sentiment and message volume">'
        + "".join(parts) + "</svg>"
    )


def sparkline(values: list[int]) -> Markup:
    w, h = 92, 28
    vals = values or [0]
    top = max(max(vals), 1)
    count = max(len(vals) - 1, 1)

    def x(i: int) -> float:
        return 2 + (w - 4) * i / count

    def y(v: int) -> float:
        return h - 3 - (h - 6) * v / top

    d = " ".join(f'{"M" if i == 0 else "L"}{x(i):.1f} {y(v):.1f}' for i, v in enumerate(vals))
    last = len(vals) - 1
    return Markup(
        f'<svg class="spark" viewBox="0 0 {w} {h}" aria-hidden="true">'
        f'<path class="a" d="{d} L{x(last):.1f} {h} L{x(0):.1f} {h} Z"/><path class="l" d="{d}"/>'
        f'<circle cx="{x(last):.1f}" cy="{y(vals[-1]):.1f}" r="2.4"/></svg>'
    )
```

Create `pulse/web/queries.py`:

```python
"""Read-only queries used only by the dashboard."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from pulse import stats
from pulse.models import to_iso
from pulse.modqueue import list_open
from pulse.theme_status import LABELS, statuses
from pulse.web.cards import cards_by_ids
from pulse.web.charts import sparkline
from pulse.web.filters import Filters

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
```

Replace `pulse/web/views/overview.py` with:

```python
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from markupsafe import Markup

from pulse import stats
from pulse.citations import render_html
from pulse.web import queries
from pulse.web.cards import cards_by_ids
from pulse.web.charts import sentiment_chart
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def overview(request: Request, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    now = f.now
    week_start = now - timedelta(days=7)
    week = stats.period_summary(conn, week_start, now, channels=f.channels)
    prev = stats.period_summary(conn, week_start - timedelta(days=7), week_start, channels=f.channels)
    window = stats.period_summary(conn, f.start, f.end, channels=f.channels)
    queue = queries.queue_cards(conn, f)
    praise = stats.sample_messages(
        conn, f.start, f.end, kinds=("praise",), most_negative=False, limit=3, channels=f.channels
    )
    digest = queries.latest_digest(conn)
    return render(
        request, "overview.html", conn, f, "overview",
        week=week,
        prev=prev,
        vol_delta=queries.pct_change(week["messages"], prev["messages"]),
        replies=stats.reply_stats(conn, week_start, now, now, channels=f.channels),
        chart=sentiment_chart(
            stats.sentiment_series(conn, f.start, f.end, channels=f.channels),
            queries.launch_markers(conn, f.start, f.end),
        ),
        rising=queries.theme_rows(conn, f, limit=5),
        mix=queries.kind_mix(window["by_kind"]),
        breakdown=[c for c in stats.channel_breakdown(conn, f.start, f.end, now) if f.channel in (None, c["id"])],
        queue_total=len(queue),
        frustrated=sum(1 for q in queue if q["reason"] == "frustrated"),
        oldest=min((q["created_at"] for q in queue), default=None),
        attention=queue[:3],
        praise=cards_by_ids(conn, [m["message_id"] for m in praise]),
        digest=digest,
        digest_html=Markup(render_html(digest["markdown"], conn)) if digest else None,
    )
```

Replace `pulse/web/templates/overview.html` with:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card, range_selector, status_pill %}
{% block title %}Overview{% endblock %}
{% block main %}
<div class="head">
  <div><h1>Community pulse</h1><p class="sub">Last {{ f.days }} days · as of {{ now.strftime('%b') }} {{ now.day }}, {{ now.strftime('%H:%M') }} UTC</p></div>
  {{ range_selector(f, "/") }}
</div>

<div class="strip">
  <div class="stat"><span class="label">Messages, 7 days</span><span class="big">{{ week.messages }}</span>
    <span class="delta {{ 'warnc' if vol_delta and vol_delta > 0 else 'mut' }}">{% if vol_delta is none %}nothing the week before{% else %}{{ '%+d'|format(vol_delta) }}% vs prev 7d{% endif %}</span></div>
  <div class="stat"><span class="label">Avg sentiment</span><span class="big {{ 'neg' if (week.avg_sentiment or 0) < 0 else 'pos' if (week.avg_sentiment or 0) > 0 else '' }}">{{ week.avg_sentiment|signed }}</span>
    <span class="delta mut">{{ prev.avg_sentiment|signed }} the week before</span></div>
  <div class="stat"><span class="label">Needs a reply</span><span class="big {{ 'neg' if queue_total else '' }}">{{ queue_total }}</span>
    <span class="delta mut">{% if queue_total %}{{ frustrated }} frustrated · oldest {{ oldest|age(now) }}{% else %}queue is clear{% endif %}</span></div>
  <div class="stat"><span class="label">First staff reply</span><span class="big">{{ replies.median_minutes|minutes }}</span>
    <span class="delta {{ 'neg' if replies.waiting_over_24h else 'mut' }}">{{ replies.answered }} of {{ replies.needs_reply }} answered · {{ replies.waiting_over_24h }} waiting &gt;24h</span></div>
  <div class="stat"><span class="label">Spend today</span><span class="big">${{ "%.2f"|format(spent) }}</span>
    <span class="delta mut">of ${{ "%.2f"|format(cap) }} daily cap</span></div>
</div>

<div class="grid2">
  <div class="stack">
    <section class="panel" aria-labelledby="h-chart">
      <div class="panel-head"><h2 id="h-chart">Sentiment and volume</h2><span class="note">daily average, −2 to +2 scale</span></div>
      <div class="chart-wrap">{{ chart }}</div>
      <div class="legend"><span><i style="background:var(--accent)"></i>avg sentiment</span><span><i style="background:var(--line);height:8px"></i>messages per day</span><span><i style="background:var(--warn)"></i>launch</span></div>
    </section>
    <section class="panel" aria-labelledby="h-where">
      <div class="panel-head"><h2 id="h-where">Where it's coming from</h2><span class="note">last {{ f.days }} days · community messages</span></div>
      {% if mix %}
      <div class="mix" role="img" aria-label="Message mix by type">{% for k in mix %}<span style="flex:{{ k.count }};background:{{ k.color }}" title="{{ k.kind|kind_label }}: {{ k.count }}"></span>{% endfor %}</div>
      <div class="mix-legend">{% for k in mix %}<span><i style="background:{{ k.color }}"></i>{{ k.kind|kind_label }}<b>{{ k.pct }}%</b></span>{% endfor %}</div>
      <div class="tbl-wrap" style="margin-top:14px"><table class="chan">
        <thead><tr><th>Channel</th><th class="num">Msgs</th><th class="num">Avg</th><th>Negative</th><th class="num">Need reply</th><th class="num">Median reply</th><th class="num">Wait &gt;24h</th></tr></thead>
        <tbody>
        {% for c in breakdown %}
          {% set share = (c.negative_share * 100)|round|int %}
          <tr>
            <td class="where"><a href="/{{ f.qs(channel=c.id) }}">#{{ c.name }}</a></td>
            <td class="num">{{ c.messages }}</td>
            <td class="num {{ 'neg' if (c.avg_sentiment or 0) < -0.2 else 'pos' if (c.avg_sentiment or 0) > 0.2 else 'mut' }}">{{ c.avg_sentiment|signed }}</td>
            <td><div class="share"><div class="track"><div class="fill" style="width:{{ share }}%"></div></div>{{ share }}%</div></td>
            <td class="num">{{ c.needs_reply }}</td>
            <td class="num">{{ c.median_reply_minutes|minutes }}</td>
            <td class="num {{ 'neg' if c.waiting_over_24h else 'mut' }}">{{ c.waiting_over_24h }}</td>
          </tr>
        {% endfor %}
        </tbody>
      </table></div>
      {% else %}<p class="empty">No community messages in this window yet.</p>{% endif %}
    </section>
  </div>
  <section class="panel" aria-labelledby="h-rising">
    <div class="panel-head"><h2 id="h-rising">Rising pain points</h2><a class="btn" href="/pain{{ f.qs() }}">All pain points</a></div>
    <div class="pain">
      {% for p in rising %}
      <div class="pain-row">
        <span class="rank">{{ '%02d'|format(loop.index) }}</span>
        <div>
          <a class="pname row-btn" href="/pain{{ f.qs(theme=p.id) }}">{{ p.name }}</a>
          <div class="pmeta">{{ p.volume }} msgs · {{ p.trend_text }}</div>
          {% if p.status != 'new' %}<div style="margin-top:4px">{{ status_pill(p.status, p.status_label) }}</div>{% endif %}
        </div>
        {{ p.spark }}
        <span class="score">{{ p.score|round|int }}</span>
      </div>
      {% else %}
      <p class="empty">No pain points for this filter yet. They appear after the theme stage runs.</p>
      {% endfor %}
    </div>
  </section>
</div>

<div class="grid2">
  <section class="panel" aria-labelledby="h-attn">
    <div class="panel-head"><h2 id="h-attn">Needs attention</h2><a class="btn" href="/queue{{ f.qs() }}">Open mod queue</a></div>
    <div class="msgs">
      {% for m in attention %}{{ message_card(m, now, f, reason=m.reason) }}{% else %}<p class="empty">Nothing is waiting for a reply.</p>{% endfor %}
    </div>
  </section>
  <section class="panel" aria-labelledby="h-well">
    <div class="panel-head"><h2 id="h-well">Landing well</h2><span class="note">most positive in this window</span></div>
    <div class="msgs">
      {% for m in praise %}{{ message_card(m, now, f) }}{% else %}<p class="empty">No praise in this window yet.</p>{% endfor %}
    </div>
  </section>
</div>

{% if digest %}
<section class="panel" aria-labelledby="h-digest">
  <div class="panel-head"><h2 id="h-digest">Latest {{ digest.kind }} digest</h2><a class="btn" href="/reports/digest/{{ digest.id }}{{ f.qs() }}">Read it all</a></div>
  <div class="report excerpt">{{ digest_html }}</div>
</section>
{% endif %}
{% endblock %}
```

Append to `pulse/web/static/pulse.css`:

```css
.stack{display:flex;flex-direction:column;gap:20px;min-width:0}
.mix{display:flex;height:12px;border-radius:6px;overflow:hidden;gap:2px}
.mix span{display:block;height:100%}
.mix-legend{display:flex;flex-wrap:wrap;gap:6px 16px;margin-top:10px;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.mix-legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px;vertical-align:-1px}
.mix-legend b{color:var(--ink);font-weight:600;margin-left:4px}
table.chan{min-width:540px}
table.chan th,table.chan td{padding-inline:8px}
table.chan th{white-space:nowrap}
table.chan a{color:var(--ink);text-decoration:none;font-family:var(--mono)}
.share{display:flex;align-items:center;gap:8px;font:500 12px var(--mono);font-variant-numeric:tabular-nums}
.share .track{flex:1;max-width:64px;min-width:40px;height:6px;border-radius:3px;background:var(--sunk);overflow:hidden}
.share .fill{height:100%;background:var(--neg)}
.excerpt{max-height:240px;overflow:hidden;-webkit-mask-image:linear-gradient(#000 70%,transparent);mask-image:linear-gradient(#000 70%,transparent)}
a.pname{display:block}
```

(If the mockup CSS already contains any of these selectors, keep the mockup's rule and skip the duplicate here.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_modqueue.py tests/test_charts.py tests/test_web_overview.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 5: Commit**

```bash
git add pulse/modqueue.py pulse/web tests/test_modqueue.py tests/test_charts.py tests/test_web_overview.py
git commit -m "feat: dashboard overview with SVG charts, reply times and channel breakdown" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 6: Pain points view with status

**Files:**
- Create: `pulse/web/views/pain.py`, `pulse/web/templates/pain.html`
- Modify: `pulse/web/app.py` (register the router)
- Test: `tests/test_web_pain.py`

**Interfaces:**
- Consumes: `queries.theme_rows`, `stats.sample_messages(theme_id=, channels=)`, `stats.theme_resolution`, `theme_status.set_status/status_for/shipped_comparison/STATUSES/LABELS`, `cards_by_ids`, `render`.
- Produces:
  - `GET /pain?theme=<id>&saved=1` (selected theme defaults to the top-ranked one; an unknown or non-numeric `theme` falls back to it; a merged theme id resolves to its root).
  - `POST /pain/{theme_id}/status` form fields `status`, `note` → 303 to `/pain?...&theme=<root>&saved=1`; 400 for an unknown status; 404 for an unknown theme.
  - Template `pain.html` with block `ask_why` (an empty block that Task 10 fills; it lives inside the right-hand panel).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_pain.py`:

```python
from tests.web_fakes import make_client


def test_pain_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/pain")
    assert r.status_code == 200 and "No pain points for this filter yet" in r.text


def test_pain_lists_themes_and_selects_top_one(tmp_path):
    html = make_client(tmp_path).get("/pain").text
    assert "Install fails on M1" in html and "Auth docs" in html
    assert 'id="msg-q1"' in html and 'id="msg-q3"' in html and 'id="msg-q2"' not in html
    assert 'name="status"' in html and "Not triaged" in html


def test_pain_selects_requested_theme(tmp_path):
    html = make_client(tmp_path).get("/pain?theme=2").text
    assert 'id="msg-q2"' in html and 'id="msg-q1"' not in html


def test_pain_unknown_theme_falls_back(tmp_path):
    client = make_client(tmp_path)
    for value in ("999", "abc", "-1"):
        r = client.get(f"/pain?theme={value}")
        assert r.status_code == 200 and 'id="msg-q1"' in r.text


def test_set_status_redirects_and_shows_it(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/pain/1/status?days=14", data={"status": "in_progress", "note": "wheels building"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pain?days=14&theme=1&saved=1"
    html = client.get(r.headers["location"]).text
    assert "Fix in progress" in html and "wheels building" in html and "Saved." in html


def test_set_status_rejects_bad_input(tmp_path):
    client = make_client(tmp_path)
    assert client.post("/pain/1/status", data={"status": "done", "note": ""}).status_code == 400
    assert client.post("/pain/999/status", data={"status": "acknowledged", "note": ""}).status_code == 404


def test_shipped_theme_shows_before_and_after(tmp_path):
    client = make_client(tmp_path)
    client.post("/pain/1/status", data={"status": "shipped", "note": "2.0.2"})
    html = client.get("/pain?theme=1").text
    assert "Fix shipped" in html and "Before" in html and "After" in html


def test_merged_theme_resolves_to_root(tmp_path):
    client = make_client(tmp_path)
    from pulse.db import connect
    conn = connect(tmp_path / "pulse.db")
    with conn:
        conn.execute("UPDATE themes SET status = 'merged', merged_into = 1 WHERE id = 2")
    conn.close()
    html = client.get("/pain?theme=2").text
    assert 'id="msg-q2"' in html and 'id="msg-q1"' in html  # root theme 1 now includes q2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_pain.py -q`
Expected: FAIL with 404 on `/pain`.

- [ ] **Step 3: Implement**

Create `pulse/web/views/pain.py`:

```python
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pulse import stats
from pulse.theme_status import LABELS, STATUSES, set_status, shipped_comparison, status_for
from pulse.web import queries
from pulse.web.cards import cards_by_ids
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


def _selected(conn, theme: str | None, rows: list[dict]) -> dict | None:
    resolved = stats.theme_resolution(conn)
    try:
        wanted = int(theme) if theme else None
    except ValueError:
        wanted = None
    if wanted is not None and wanted in resolved:
        root = resolved[wanted]
        for row in rows:
            if row["id"] == root:
                return row
        t = conn.execute("SELECT id, name, description FROM themes WHERE id = ?", (root,)).fetchone()
        st = status_for(conn, root)
        return {
            "id": t["id"], "name": t["name"], "description": t["description"], "volume": 0,
            "kinds": {}, "status": st["status"], "status_label": st["label"], "note": st["note"],
        }
    return rows[0] if rows else None


@router.get("/pain", response_class=HTMLResponse)
def pain(
    request: Request,
    theme: str | None = None,
    saved: str | None = None,
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    rows = queries.theme_rows(conn, f, limit=50)
    sel = _selected(conn, theme, rows)
    evidence, comparison = [], None
    if sel is not None:
        sample = stats.sample_messages(conn, f.start, f.end, theme_id=sel["id"], limit=8, channels=f.channels)
        evidence = cards_by_ids(conn, [m["message_id"] for m in sample])
        comparison = shipped_comparison(conn, sel["id"], f.now)
    return render(
        request, "pain.html", conn, f, "pain",
        rows=rows, sel=sel, evidence=evidence, comparison=comparison,
        statuses=[(s, LABELS[s]) for s in STATUSES], saved=bool(saved),
    )


@router.post("/pain/{theme_id}/status")
def save_status(
    request: Request,
    theme_id: int,
    status: str = Form(...),
    note: str = Form(""),
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    try:
        set_status(conn, theme_id, status, note, f.now)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    root = stats.theme_resolution(conn)[theme_id]
    return RedirectResponse(f"/pain{f.qs(theme=root, saved=1)}", status_code=303)
```

In `pulse/web/app.py`, change the views import to `from pulse.web.views import overview, pain` and the router tuple to `(overview, pain)`.

Create `pulse/web/templates/pain.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card, range_selector, status_pill %}
{% block title %}Pain points{% endblock %}
{% block main %}
<div class="head">
  <div><h1>Pain points</h1><p class="sub">Score = messages in the last 7 days × mean negativity × (1 + growth vs the 7 days before)</p></div>
  {{ range_selector(f, "/pain", {"theme": sel.id if sel else ""}) }}
</div>

{% if not rows and not sel %}
<p class="empty">No pain points for this filter yet. They appear after the theme stage runs (<code>python -m pulse.run themes</code>).</p>
{% else %}
<section class="panel">
  <div class="tbl-wrap"><table>
    <thead><tr><th>Pain point</th><th>Status</th><th>Trend, {{ f.days }}d</th><th class="num">Msgs 7d</th><th class="num">Prev 7d</th><th class="num">Negativity</th><th class="num">Score</th></tr></thead>
    <tbody>
    {% for p in rows %}
      <tr{% if sel and p.id == sel.id %} class="sel"{% endif %}>
        <td><a class="row-btn" href="/pain{{ f.qs(theme=p.id) }}">{{ p.name }}</a>
          <div class="pmeta">{% if p.prev_volume == 0 %}<span class="pill rising">new this week</span>{% elif p.trend > 1 %}<span class="pill rising">rising</span>{% endif %}</div></td>
        <td>{{ status_pill(p.status, p.status_label) }}</td>
        <td>{{ p.spark }}</td>
        <td class="num">{{ p.volume }}</td>
        <td class="num">{{ p.prev_volume }}</td>
        <td class="num">{{ "%.2f"|format(p.negativity) }}</td>
        <td class="num"><b>{{ p.score|round|int }}</b></td>
      </tr>
    {% endfor %}
    </tbody>
  </table></div>
</section>

{% if sel %}
<div class="grid2">
  <section class="panel" aria-labelledby="h-ev">
    <div class="panel-head"><h2 id="h-ev">{{ sel.name }}</h2><span class="note">{{ evidence|length }} messages shown · most negative first</span></div>
    {% if sel.description %}<p class="mut" style="margin:-6px 0 12px">{{ sel.description }}</p>{% endif %}
    <form class="status-row" method="post" action="/pain/{{ sel.id }}/status{{ f.qs() }}">
      <label for="st-sel" class="label">Status</label>
      <select id="st-sel" name="status">
        {% for value, label in statuses %}<option value="{{ value }}"{% if sel.status == value %} selected{% endif %}>{{ label }}</option>{% endfor %}
      </select>
      <input id="st-note" type="text" name="note" value="{{ sel.note }}" maxlength="500" placeholder="Note for the team (optional)" aria-label="Status note">
      <button class="btn" type="submit">Save</button>
      {% if saved %}<span class="pmeta" role="status">Saved.</span>{% endif %}
    </form>
    {% if comparison %}
    <div class="compare" style="margin-bottom:14px">
      <div class="inv"><span class="label">Before ({{ comparison.days }} days)</span><p>{{ comparison.before.messages }} messages · avg {{ comparison.before.avg_sentiment|signed }}</p></div>
      <div class="inv"><span class="label">After ({{ comparison.days }} days)</span><p>{{ comparison.after.messages }} messages · avg {{ comparison.after.avg_sentiment|signed }}</p></div>
    </div>
    {% endif %}
    <div class="msgs">
      {% for m in evidence %}{{ message_card(m, now, f) }}{% else %}<p class="empty">No messages for this pain point in this window and channel.</p>{% endfor %}
    </div>
  </section>
  <section class="panel" aria-labelledby="h-side">
    <div class="panel-head"><h2 id="h-side">Message types</h2></div>
    {% if sel.kinds %}
    <div class="mix-legend" style="margin:0 0 14px">{% for k, n in sel.kinds.items() %}<span>{{ k|kind_label }}<b>{{ n }}</b></span>{% endfor %}</div>
    {% else %}<p class="empty">No messages in the last 7 days.</p>{% endif %}
    <p><a href="/messages{{ f.qs(theme=sel.id) }}">All messages in this pain point</a> · <a href="/bugs{{ f.qs() }}">Bugs</a></p>
    {% block ask_why %}{% endblock %}
  </section>
</div>
{% endif %}
{% endif %}
{% endblock %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_pain.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 5: Commit**

```bash
git add pulse/web tests/test_web_pain.py
git commit -m "feat: pain points view with status, evidence and before/after when shipped" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 7: Bugs and Mod queue views

**Files:**
- Modify: `pulse/modqueue.py`, `pulse/web/queries.py`, `pulse/web/app.py`, `pulse/web/static/pulse.css`
- Create: `pulse/web/views/bugs.py`, `pulse/web/views/queue.py`, `pulse/web/templates/bugs.html`, `pulse/web/templates/queue.html`, `pulse/web/templates/_queue_closed.html`
- Test: `tests/test_modqueue.py` (append), `tests/test_web_bugs_queue.py`

**Interfaces:**
- Consumes: `stats.first_team_reply`, `stats.theme_resolution`, `stats.theme_scores(channels=)`, `queries.queue_cards`, `cards_by_ids`, `render`.
- Produces:
  - `modqueue.close_item(conn, queue_id: int, status: str, now: datetime, closed_by: str = "dashboard") -> bool` (`ValueError` unless status is `handled` or `dismissed`; `False` when the item is missing or not open).
  - `queries.bug_groups(conn, f, *, open_only=False, per_group=5) -> list[dict]` keys `theme_id, name, count, people, unanswered, first, latest, cards, unanswered_ids, more`; groups ordered by the pain point ranking, then size; bugs with no theme last under "Not yet grouped".
  - `GET /bugs?open=1`, `GET /queue?reason=frustrated|unanswered`, `POST /queue/{queue_id}/close` (form field `action`; 303 back to the queue, or the `_queue_closed.html` fragment when the request has an `HX-Request` header; 404 when already closed or missing; 400 for an unknown action).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_modqueue.py`:

```python
def test_close_item_closes_once():
    from pulse.modqueue import close_item

    conn = connect(":memory:")
    upsert_messages(conn, [msg("a", "broken", minutes=0)], frozenset())
    set_triage(conn, "a", sentiment=-2, kind="bug")
    refresh_mod_queue(conn, make_config(), T0 + timedelta(hours=1))
    qid = conn.execute("SELECT id FROM mod_queue").fetchone()[0]
    assert close_item(conn, qid, "handled", T0 + timedelta(hours=2)) is True
    row = conn.execute("SELECT status, closed_by, closed_at FROM mod_queue WHERE id = ?", (qid,)).fetchone()
    assert (row["status"], row["closed_by"]) == ("handled", "dashboard")
    assert row["closed_at"] == to_iso(T0 + timedelta(hours=2))
    assert close_item(conn, qid, "dismissed", T0 + timedelta(hours=3)) is False
    assert close_item(conn, 999, "handled", T0) is False
    import pytest
    with pytest.raises(ValueError):
        close_item(conn, qid, "open", T0)
```

Create `tests/test_web_bugs_queue.py`:

```python
from pulse.db import connect
from tests.web_fakes import make_client


def test_bugs_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/bugs")
    assert r.status_code == 200 and "No bug reports in this window" in r.text


def test_bugs_grouped_by_pain_point_with_reply_state(tmp_path):
    html = make_client(tmp_path).get("/bugs").text
    install = html.index("Install fails on M1")
    ungrouped = html.index("Not yet grouped")
    assert install < ungrouped
    assert "2 reports" in html and "2 without staff reply" in html
    assert 'id="msg-q1"' in html and 'id="msg-q3"' in html and 'id="msg-g2"' in html
    assert 'id="msg-q2"' not in html  # docs, not a bug


def test_bugs_channel_filter(tmp_path):
    html = make_client(tmp_path).get("/bugs?channel=200").text
    assert 'id="msg-g2"' in html and 'id="msg-q1"' not in html


def test_queue_lists_open_items_in_priority_order(tmp_path):
    html = make_client(tmp_path).get("/queue").text
    assert html.index('id="msg-q1"') < html.index('id="msg-g2"') < html.index('id="msg-q3"')
    assert "Mark handled" in html and "Dismiss" in html


def test_queue_reason_filter(tmp_path):
    html = make_client(tmp_path).get("/queue?reason=unanswered").text
    assert 'id="msg-q3"' in html and 'id="msg-q1"' not in html


def test_queue_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/queue")
    assert r.status_code == 200 and "Nothing is waiting for a reply" in r.text


def _qid(tmp_path, message_id):
    conn = connect(tmp_path / "pulse.db")
    qid = conn.execute("SELECT id FROM mod_queue WHERE message_id = ?", (message_id,)).fetchone()[0]
    conn.close()
    return qid


def test_close_from_form_redirects_and_removes_item(tmp_path):
    client = make_client(tmp_path)
    qid = _qid(tmp_path, "q1")
    r = client.post(f"/queue/{qid}/close?days=14", data={"action": "handled"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/queue?days=14"
    assert 'id="msg-q1"' not in client.get("/queue").text


def test_close_from_htmx_returns_fragment(tmp_path):
    client = make_client(tmp_path)
    r = client.post(f"/queue/{_qid(tmp_path, 'g2')}/close", data={"action": "dismissed"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Dismissed" in r.text and 'id="msg-g2"' in r.text


def test_close_already_closed_item_is_404(tmp_path):
    client = make_client(tmp_path)
    qid = _qid(tmp_path, "q3")
    assert client.post(f"/queue/{qid}/close", data={"action": "handled"}).status_code in (200, 303)
    assert client.post(f"/queue/{qid}/close", data={"action": "handled"}).status_code == 404
    assert client.post("/queue/99999/close", data={"action": "handled"}).status_code == 404
    assert client.post(f"/queue/{qid}/close", data={"action": "reopen"}).status_code == 400
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_modqueue.py tests/test_web_bugs_queue.py -q`
Expected: FAIL (`cannot import name 'close_item'`; 404 on `/bugs` and `/queue`).

- [ ] **Step 3: Implement**

Add to `pulse/modqueue.py`:

```python
def close_item(
    conn: sqlite3.Connection, queue_id: int, status: str, now: datetime, closed_by: str = "dashboard"
) -> bool:
    """Close an open item as handled or dismissed. Returns False if it is missing or already closed."""
    if status not in ("handled", "dismissed"):
        raise ValueError(f"status must be handled or dismissed, got {status!r}")
    with conn:
        cur = conn.execute(
            "UPDATE mod_queue SET status = ?, closed_at = ?, closed_by = ? WHERE id = ? AND status = 'open'",
            (status, to_iso(now), closed_by, queue_id),
        )
    return cur.rowcount == 1
```

Add to `pulse/web/queries.py`:

```python
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
```

Create `pulse/web/views/bugs.py`:

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse.web import queries
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


@router.get("/bugs", response_class=HTMLResponse)
def bugs(request: Request, open: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    open_only = open == "1"
    return render(
        request, "bugs.html", conn, f, "bugs",
        groups=queries.bug_groups(conn, f, open_only=open_only), open_only=open_only,
    )
```

Create `pulse/web/views/queue.py`:

```python
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pulse.modqueue import close_item
from pulse.web import queries
from pulse.web.cards import cards_by_ids
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()
REASONS = ("frustrated", "unanswered")


@router.get("/queue", response_class=HTMLResponse)
def queue(request: Request, reason: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    reason = reason if reason in REASONS else None
    return render(request, "queue.html", conn, f, "queue", items=queries.queue_cards(conn, f, reason=reason), reason=reason)


@router.post("/queue/{queue_id}/close")
def close(
    request: Request,
    queue_id: int,
    action: str = Form(...),
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    if action not in ("handled", "dismissed"):
        raise HTTPException(status_code=400, detail="action must be handled or dismissed")
    row = conn.execute("SELECT message_id FROM mod_queue WHERE id = ?", (queue_id,)).fetchone()
    if row is None or not close_item(conn, queue_id, action, f.now):
        raise HTTPException(status_code=404, detail="This item is already closed or does not exist.")
    if request.headers.get("HX-Request"):
        card = cards_by_ids(conn, [row["message_id"]])[0]
        return request.app.state.templates.TemplateResponse(
            request, "_queue_closed.html", {"m": card, "action": action}
        )
    return RedirectResponse(f"/queue{f.qs()}", status_code=303)
```

In `pulse/web/app.py`, import `bugs` and `queue` from `pulse.web.views` and add them to the router tuple: `(overview, pain, bugs, queue)`.

Create `pulse/web/templates/bugs.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card, range_selector %}
{% block title %}Bugs{% endblock %}
{% block main %}
<div class="head">
  <div><h1>Bugs</h1><p class="sub">Bug reports from every channel, grouped by pain point and ranked by it. Unanswered ones are also in the mod queue.</p></div>
  <div class="form-row">
    <div class="chips" role="group" aria-label="Filter bugs">
      <a class="chip" href="/bugs{{ f.qs() }}"{% if not open_only %} aria-current="true"{% endif %}>All</a>
      <a class="chip" href="/bugs{{ f.qs(open=1) }}"{% if open_only %} aria-current="true"{% endif %}>No staff reply</a>
    </div>
    {{ range_selector(f, "/bugs", {"open": 1 if open_only else ""}) }}
  </div>
</div>
<div class="bug-groups">
{% for g in groups %}
  <section class="panel bug-group">
    <h3>{{ g.name }} <span class="pill kind">{{ g.count }} {{ 'report' if g.count == 1 else 'reports' }}</span>
      {% if g.unanswered %}<span class="pill unanswered">{{ g.unanswered }} without staff reply</span>{% else %}<span class="pill ok">all answered</span>{% endif %}</h3>
    <p class="pmeta">{{ g.people }} {{ 'person' if g.people == 1 else 'people' }} · first {{ g.first|age(now) }} · latest {{ g.latest|age(now) }}
      {% if g.theme_id %} · <a href="/pain{{ f.qs(theme=g.theme_id) }}">pain point</a>{% endif %}</p>
    <div class="msgs">
      {% for m in g.cards %}{{ message_card(m, now, f, reason='unanswered' if m.message_id in g.unanswered_ids else None) }}{% endfor %}
    </div>
    {% if g.more %}<p class="pmeta" style="margin-top:8px"><a href="/messages{{ f.qs(kind='bug', theme=g.theme_id or '') }}">{{ g.more }} more</a></p>{% endif %}
  </section>
{% else %}
  <p class="empty">No bug reports in this window{% if open_only %} without a staff reply{% endif %}.</p>
{% endfor %}
</div>
{% endblock %}
```

Create `pulse/web/templates/queue.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card %}
{% block title %}Mod queue{% endblock %}
{% block main %}
<div class="head">
  <div><h1>Mod queue</h1><p class="sub">Frustrated users first, then most likely to need a reply, oldest first. Items close by themselves when staff reply in the thread.</p></div>
  <div class="chips" role="group" aria-label="Filter queue">
    <a class="chip" href="/queue{{ f.qs() }}"{% if not reason %} aria-current="true"{% endif %}>All</a>
    <a class="chip" href="/queue{{ f.qs(reason='frustrated') }}"{% if reason == 'frustrated' %} aria-current="true"{% endif %}>Frustrated</a>
    <a class="chip" href="/queue{{ f.qs(reason='unanswered') }}"{% if reason == 'unanswered' %} aria-current="true"{% endif %}>Unanswered</a>
  </div>
</div>
<div class="msgs">
{% for m in items %}
  {% call message_card(m, now, f, reason=m.reason) %}
  <form class="actions" method="post" action="/queue/{{ m.queue_id }}/close{{ f.qs() }}"
        hx-post="/queue/{{ m.queue_id }}/close{{ f.qs() }}" hx-target="#msg-{{ m.message_id }}" hx-swap="outerHTML">
    <button class="btn" type="submit" name="action" value="handled">Mark handled</button>
    <button class="btn" type="submit" name="action" value="dismissed">Dismiss</button>
  </form>
  {% endcall %}
{% else %}
  <p class="empty">Nothing is waiting for a reply.</p>
{% endfor %}
</div>
{% endblock %}
```

Create `pulse/web/templates/_queue_closed.html`:

```html
<article class="msg done" id="msg-{{ m.message_id }}">
  <div class="av" style="background:{{ m.author|avatar_color }}" aria-hidden="true">{{ m.author[:1]|upper }}</div>
  <div>
    <p class="mtext" role="status">{{ 'Marked handled' if action == 'handled' else 'Dismissed' }}: {{ m.author }} in #{{ m.channel }}</p>
    <div class="mfoot"><a href="{{ m.link }}" target="_blank" rel="noopener">Open in Discord ↗</a></div>
  </div>
</article>
```

Append to `pulse/web/static/pulse.css`:

```css
form.actions{display:flex;gap:6px;margin-left:auto}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_modqueue.py tests/test_web_bugs_queue.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 5: Commit**

```bash
git add pulse/modqueue.py pulse/web tests/test_modqueue.py tests/test_web_bugs_queue.py
git commit -m "feat: bugs grouped by pain point and a mod queue with handled/dismiss" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 8: Messages view (search and the author link target)

**Files:**
- Modify: `pulse/web/queries.py`, `pulse/web/app.py`
- Create: `pulse/web/views/messages.py`, `pulse/web/templates/messages.html`
- Test: `tests/test_web_messages.py`

**Interfaces:**
- Consumes: `stats.scope_clause`, `stats.theme_member_ids`, `stats._like` (literal-substring LIKE pattern, used with `ESCAPE '\'`), `KINDS`, `cards_by_ids`.
- Produces:
  - `queries.PER_PAGE = 50`; `queries.search_cards(conn, f, *, text=None, author_id=None, theme_id=None, kind=None, mood=None, page=1) -> tuple[list[dict], int]` (every author, staff included; newest first; `mood` in `neg | neutral | pos`; unknown `kind`/`mood` ignored).
  - `GET /messages?q=&author_id=&theme=&kind=&mood=&page=` (bad values fall back: page < 1 → 1, non-numeric theme ignored).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_messages.py`:

```python
from tests.web_fakes import make_client


def _ids(html):
    return {mid for mid in ("q1", "q2", "s1", "q3", "p1", "g1", "g2") if f'id="msg-{mid}"' in html}


def test_messages_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/messages")
    assert r.status_code == 200 and "No messages match" in r.text


def test_messages_lists_everyone_newest_first(tmp_path):
    html = make_client(tmp_path).get("/messages").text
    assert _ids(html) == {"q1", "q2", "s1", "q3", "p1", "g1", "g2"}
    assert html.index('id="msg-g2"') < html.index('id="msg-q1"')
    assert "7 messages" in html


def test_messages_by_author(tmp_path):
    html = make_client(tmp_path).get("/messages?author_id=u1").text
    assert _ids(html) == {"q1"} and "Messages from alice" in html


def test_messages_text_kind_theme_and_mood_filters(tmp_path):
    client = make_client(tmp_path)
    assert _ids(client.get("/messages?q=wheel").text) == {"q3"}
    assert _ids(client.get("/messages?q=%25").text) == set()  # a literal %, not a wildcard
    assert _ids(client.get("/messages?kind=bug").text) == {"q1", "q3", "g2"}
    assert _ids(client.get("/messages?theme=1").text) == {"q1", "q3"}
    assert _ids(client.get("/messages?mood=pos").text) == {"p1"}


def test_messages_bad_params(tmp_path):
    r = make_client(tmp_path).get("/messages?page=-3&theme=abc&kind=zzz&mood=x")
    assert r.status_code == 200 and len(_ids(r.text)) == 7


def test_messages_second_page_is_empty_with_previous_link(tmp_path):
    html = make_client(tmp_path).get("/messages?page=2").text
    assert "No messages match" in html and "Previous" in html
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_messages.py -q`
Expected: FAIL with 404 on `/messages`.

- [ ] **Step 3: Implement**

Add to `pulse/web/queries.py` (add `from pulse.models import KINDS, to_iso` in place of the current `to_iso` import):

```python
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
```

Create `pulse/web/views/messages.py`:

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse.models import KINDS
from pulse.web import queries
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


@router.get("/messages", response_class=HTMLResponse)
def messages(
    request: Request,
    q: str | None = None,
    author_id: str | None = None,
    theme: str | None = None,
    kind: str | None = None,
    mood: str | None = None,
    page: str | None = None,
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    page_no = max(_int(page) or 1, 1)
    theme_id = _int(theme)
    kind = kind if kind in KINDS else None
    mood = mood if mood in queries.MOODS else None
    cards, total = queries.search_cards(
        conn, f, text=(q or "").strip() or None, author_id=author_id or None,
        theme_id=theme_id, kind=kind, mood=mood, page=page_no,
    )
    author = None
    if author_id:
        row = conn.execute("SELECT author_name FROM messages WHERE author_id = ? LIMIT 1", (author_id,)).fetchone()
        author = row["author_name"] if row else author_id
    theme_name = None
    if theme_id is not None:
        row = conn.execute("SELECT name FROM themes WHERE id = ?", (theme_id,)).fetchone()
        theme_name = row["name"] if row else None
    keep = {"q": q or "", "author_id": author_id or "", "theme": theme_id or "", "kind": kind or "", "mood": mood or ""}
    return render(
        request, "messages.html", conn, f, "messages",
        cards=cards, total=total, page=page_no, per_page=queries.PER_PAGE, keep=keep,
        author=author, theme_name=theme_name, kinds=KINDS,
    )
```

In `pulse/web/app.py`, import `messages` from `pulse.web.views` and add it to the router tuple.

Create `pulse/web/templates/messages.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card, range_selector %}
{% block title %}Messages{% endblock %}
{% block main %}
<div class="head">
  <div>
    <h1>{% if author %}Messages from {{ author }}{% elif theme_name %}{{ theme_name }}{% else %}Messages{% endif %}</h1>
    <p class="sub">{{ total }} {{ 'message' if total == 1 else 'messages' }} · last {{ f.days }} days · newest first</p>
  </div>
  {{ range_selector(f, "/messages", keep) }}
</div>
<form class="form-row" method="get" action="/messages">
  <input type="hidden" name="days" value="{{ f.days }}">
  {% if f.channel %}<input type="hidden" name="channel" value="{{ f.channel }}">{% endif %}
  {% if keep.author_id %}<input type="hidden" name="author_id" value="{{ keep.author_id }}">{% endif %}
  {% if keep.theme %}<input type="hidden" name="theme" value="{{ keep.theme }}">{% endif %}
  <input id="msg-q" type="search" name="q" value="{{ keep.q }}" placeholder="Search message text" aria-label="Search message text">
  <select id="msg-kind" name="kind" aria-label="Kind">
    <option value="">Any kind</option>
    {% for k in kinds %}<option value="{{ k }}"{% if keep.kind == k %} selected{% endif %}>{{ k|kind_label }}</option>{% endfor %}
  </select>
  <select id="msg-mood" name="mood" aria-label="Sentiment">
    <option value="">Any sentiment</option>
    <option value="neg"{% if keep.mood == 'neg' %} selected{% endif %}>Negative</option>
    <option value="neutral"{% if keep.mood == 'neutral' %} selected{% endif %}>Neutral</option>
    <option value="pos"{% if keep.mood == 'pos' %} selected{% endif %}>Positive</option>
  </select>
  <button class="btn" type="submit">Search</button>
  {% if keep.q or keep.kind or keep.mood or keep.author_id or keep.theme %}<a class="btn" href="/messages{{ f.qs() }}">Clear</a>{% endif %}
</form>
<div class="msgs">
  {% for m in cards %}{{ message_card(m, now, f) }}{% else %}<p class="empty">No messages match these filters.</p>{% endfor %}
</div>
<div class="pager">
  {% if page > 1 %}<a class="btn" href="/messages{{ f.qs(page=page - 1, **keep) }}">Previous</a>{% endif %}
  {% if page * per_page < total %}<a class="btn" href="/messages{{ f.qs(page=page + 1, **keep) }}">Next</a>{% endif %}
</div>
{% endblock %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_messages.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 5: Commit**

```bash
git add pulse/web tests/test_web_messages.py
git commit -m "feat: messages view with text, author, theme, kind and sentiment filters" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 9: Reports and Launch views, background digests, stored removed citations

**Files:**
- Modify: `pulse/db.py`, `pulse/agents/digest.py`, `pulse/web/queries.py`, `pulse/web/app.py`, `tests/web_fakes.py`
- Create: `pulse/web/jobs.py`, `pulse/web/views/reports.py`, `pulse/web/views/launch.py`, `pulse/web/templates/reports.html`, `pulse/web/templates/report_digest.html`, `pulse/web/templates/report_investigation.html`, `pulse/web/templates/launch.html`
- Test: `tests/test_digest.py` (append), `tests/test_web_reports_launch.py`

**Interfaces:**
- Consumes: `run_digest`, `build_launch_input` (digest agent); `render_html`; `sentiment_chart`; `cards_by_ids`; `stats.sentiment_series`; `settings.llm_factory`, `settings.agents_on`.
- Produces:
  - `digests.removed_citations` and `investigations.removed_citations` (`TEXT NOT NULL DEFAULT '[]'`, in `CREATE TABLE` and in `_ADDED_COLUMNS`); `run_digest` stores the removed ids there.
  - `pulse.web.jobs.run_digest_job(settings, launch: str | None) -> None` (own connection; logs and swallows `LookupError`, `BudgetExceeded`, `LLMError`).
  - `queries.report_rows(conn) -> list[dict]` keys `type ("digest"|"investigation"), id, title, created_at, state ("done"|"running"|"failed"), href`; `queries.JOB_TIMEOUT_MINUTES = 10`; `queries.digest_job_state(conn, since: datetime, now: datetime) -> tuple[str, str | None]` (`"done" | "failed" | "running"`, failure text).
  - `GET /reports?pending=<epoch seconds>`, `POST /reports/digest`, `GET /reports/digest/{id}`, `GET /reports/investigation/{id}` (polls every 3 s while the investigation has no markdown), `GET /launch?name=`, `POST /launch/{launch_id}/digest`. Agent POSTs return 409 when agents are off.
  - `tests/web_fakes.fake_llm_factory(handler) -> factory` (factory has a `.backend` attribute, a `FakeBackend`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_digest.py`:

```python
def test_digest_stores_removed_citations():
    conn = seed()
    result = run_digest(conn, make_llm(conn, make_config(), FakeBackend(handler=cite_first_and_bogus)), NOW)
    row = conn.execute("SELECT removed_citations FROM digests WHERE id = ?", (result.digest_id,)).fetchone()
    assert json.loads(row["removed_citations"]) == ["bogus"]
```

Append to `tests/web_fakes.py`:

```python
def fake_llm_factory(handler):
    """An llm_factory whose LLMClient answers through a FakeBackend(handler=handler)."""
    from tests.fakes import FakeBackend, make_llm

    backend = FakeBackend(handler=handler)

    def factory(conn, config):
        return make_llm(conn, config, backend)

    factory.backend = backend
    return factory
```

Create `tests/test_web_reports_launch.py`:

```python
import json

from pulse.agents.base import BackendResult
from pulse.db import connect
from pulse.models import to_iso
from tests.fakes import make_config
from tests.web_fakes import CONFIG, NOW, fake_llm_factory, make_client


def cite_first_and_bogus(user):
    first = json.loads(user)["messages"][0]["message_id"]
    return BackendResult({"markdown": f"## What's landing well\nSee [[msg:{first}]] and [[msg:bogus]]."}, 500, 200)


def test_reports_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/reports")
    assert r.status_code == 200 and "No reports yet" in r.text
    assert "Agents are off" in r.text and "disabled" in r.text


def test_reports_list_and_digest_detail(tmp_path):
    client = make_client(tmp_path)
    html = client.get("/reports").text
    assert "Weekly digest" in html and "/reports/digest/1" in html
    detail = client.get("/reports/digest/1").text
    assert "@uma" in detail and 'class="cite"' in detail
    assert 'id="msg-p1"' in detail and 'id="msg-q1"' in detail  # cited messages as cards
    assert client.get("/reports/digest/99").status_code == 404


def test_write_weekly_digest_in_background(tmp_path):
    factory = fake_llm_factory(cite_first_and_bogus)
    client = make_client(tmp_path, llm_factory=factory)
    r = client.post("/reports/digest?days=14", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/reports?days=14&pending={int(NOW.timestamp())}"
    page = client.get(r.headers["location"]).text
    assert "Digest ready" in page and "hx-trigger" not in page
    conn = connect(tmp_path / "pulse.db")
    newest = conn.execute("SELECT id, removed_citations FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(newest["removed_citations"]) == ["bogus"]
    assert "1 citation removed" in client.get(f"/reports/digest/{newest['id']}").text


def test_pending_digest_polls_until_done(tmp_path):
    client = make_client(tmp_path, llm_factory=fake_llm_factory(cite_first_and_bogus))
    since = int(NOW.timestamp()) + 60  # nothing written after this yet
    page = client.get(f"/reports?pending={since}").text
    assert 'hx-trigger="every 5s"' in page and "Writing the digest" in page


def test_agent_buttons_refused_when_agents_off(tmp_path):
    client = make_client(tmp_path, demo=True, llm_factory=fake_llm_factory(cite_first_and_bogus))
    assert client.post("/reports/digest").status_code == 409
    assert client.post("/launch/1/digest").status_code == 409
    assert "Agents are off in demo mode" in client.get("/reports").text


def test_investigation_detail_running_and_done(tmp_path):
    client = make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    with conn:
        conn.execute("INSERT INTO investigations (id, question, context, created_at) VALUES (1, 'why?', '{}', ?)", (to_iso(NOW),))
        conn.execute(
            "INSERT INTO investigations (id, question, context, markdown, cited_message_ids, created_at)"
            " VALUES (2, 'why else?', '{}', 'Because [[msg:q1]].', '[\"q1\"]', ?)", (to_iso(NOW),))
    running = client.get("/reports/investigation/1").text
    assert 'hx-trigger="every 3s"' in running and "Investigating" in running
    done = client.get("/reports/investigation/2").text
    assert "hx-trigger" not in done and "@alice" in done and 'id="msg-q1"' in done
    assert client.get("/reports/investigation/9").status_code == 404


def test_launch_empty(tmp_path):
    r = make_client(tmp_path, seeded=False, config=make_config()).get("/launch")
    assert r.status_code == 200 and "No launches yet" in r.text


def test_launch_before_after(tmp_path):
    html = make_client(tmp_path).get("/launch").text
    assert "v2.0" in html and "<svg class=\"chart\"" in html
    assert "6 messages" in html and "0 messages" in html  # after vs before
    assert 'id="msg-q1"' in html  # keyword "install"


def test_launch_in_the_future(tmp_path):
    from pulse.config import Launch

    cfg = make_config(launches=(Launch("v3.0", "2026-12-01", ("v3",)),))
    client = make_client(tmp_path, config=cfg)
    conn = connect(tmp_path / "pulse.db")
    from pulse.store import sync_launches
    sync_launches(conn, cfg.launches)
    conn.close()
    html = client.get("/launch?name=v3.0").text
    assert "hasn't happened yet" in html


def test_launch_digest_in_background(tmp_path):
    client = make_client(tmp_path, llm_factory=fake_llm_factory(cite_first_and_bogus))
    r = client.post("/launch/1/digest", follow_redirects=False)
    assert r.status_code == 303 and "pending=" in r.headers["location"]
    conn = connect(tmp_path / "pulse.db")
    assert conn.execute("SELECT kind FROM digests ORDER BY id DESC LIMIT 1").fetchone()["kind"] == "launch"
    assert client.post("/launch/99/digest").status_code == 404
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_digest.py tests/test_web_reports_launch.py -q`
Expected: FAIL (`no such column: removed_citations`; 404 on `/reports`).

- [ ] **Step 3: Implement the data changes**

In `pulse/db.py`, add `removed_citations TEXT NOT NULL DEFAULT '[]'` as the last column of both the `digests` and the `investigations` tables in `SCHEMA` (put a comma after the previous last column), and extend `_ADDED_COLUMNS`:

```python
    "digests": (("removed_citations", "TEXT NOT NULL DEFAULT '[]'"),),
    "investigations": (("removed_citations", "TEXT NOT NULL DEFAULT '[]'"),),
```

In `pulse/agents/digest.py`, make `_save` store the removed ids:

```python
def _save(conn, kind, period, launch_id, markdown, removed, run_id, now) -> DigestResult:
    cited = cited_ids(markdown)
    with conn:
        digest_id = int(conn.execute(
            "INSERT INTO digests (kind, period_start, period_end, launch_id, markdown, cited_message_ids,"
            " removed_citations, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, period[0], period[1], launch_id, markdown, json.dumps(cited), json.dumps(removed),
             run_id, to_iso(now)),
        ).lastrowid)
    return DigestResult(digest_id, kind, period[0], period[1], markdown, cited, removed)
```

- [ ] **Step 4: Implement jobs, queries and views**

Create `pulse/web/jobs.py`:

```python
"""Agent work started from the dashboard. Each job opens its own connection."""
from __future__ import annotations

import logging

from pulse.agents.base import BudgetExceeded, LLMError
from pulse.agents.digest import run_digest
from pulse.db import connect
from pulse.web.settings import WebSettings

log = logging.getLogger(__name__)


def run_digest_job(settings: WebSettings, launch: str | None) -> None:
    conn = connect(settings.db_path)
    try:
        llm = settings.llm_factory(conn, settings.config)
        run_digest(conn, llm, settings.clock(), launch=launch)
    except (LookupError, BudgetExceeded, LLMError) as e:
        log.warning("digest job failed: %s", e)
    finally:
        conn.close()
```

Add to `pulse/web/queries.py` (add `from datetime import datetime, timedelta, timezone` if not already imported):

```python
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
```

Create `pulse/web/views/reports.py`:

```python
import json
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from markupsafe import Markup

from pulse.citations import render_html
from pulse.web import jobs, queries
from pulse.web.cards import cards_by_ids
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


def require_agents(request: Request) -> None:
    settings = request.app.state.settings
    if not settings.agents_on:
        raise HTTPException(status_code=409, detail=settings.agents_off_text)


@router.get("/reports", response_class=HTMLResponse)
def reports(request: Request, pending: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    job = None
    if pending:
        try:
            since = datetime.fromtimestamp(int(pending), timezone.utc)
        except (ValueError, OverflowError, OSError):
            since = None
        if since is not None:
            state, reason = queries.digest_job_state(conn, since, f.now)
            job = {"state": state, "reason": reason, "pending": pending}
    return render(request, "reports.html", conn, f, "reports", rows=queries.report_rows(conn), job=job)


@router.post("/reports/digest")
def write_weekly(request: Request, background: BackgroundTasks, f: Filters = Depends(get_filters)):
    require_agents(request)
    settings = request.app.state.settings
    background.add_task(jobs.run_digest_job, settings, None)
    return RedirectResponse(f"/reports{f.qs(pending=int(f.now.timestamp()))}", status_code=303)


def _cards(conn, raw: str) -> list[dict]:
    return cards_by_ids(conn, json.loads(raw or "[]"))


@router.get("/reports/digest/{digest_id}", response_class=HTMLResponse)
def digest_detail(request: Request, digest_id: int, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    row = conn.execute(
        "SELECT d.*, l.name AS launch FROM digests d LEFT JOIN launches l ON l.id = d.launch_id WHERE d.id = ?",
        (digest_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such digest")
    return render(
        request, "report_digest.html", conn, f, "reports",
        d=row, html=Markup(render_html(row["markdown"], conn)), cited=_cards(conn, row["cited_message_ids"]),
        removed=json.loads(row["removed_citations"] or "[]"),
    )


@router.get("/reports/investigation/{inv_id}", response_class=HTMLResponse)
def investigation_detail(request: Request, inv_id: int, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    row = conn.execute("SELECT * FROM investigations WHERE id = ?", (inv_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such investigation")
    md = row["markdown"]
    state = "running" if md is None else "failed" if md.startswith("Investigation failed") else "done"
    return render(
        request, "report_investigation.html", conn, f, "reports",
        inv=row, state=state,
        html=Markup(render_html(md, conn)) if state == "done" else None,
        cited=_cards(conn, row["cited_message_ids"]) if state == "done" else [],
        removed=json.loads(row["removed_citations"] or "[]"),
    )
```

Create `pulse/web/views/launch.py`:

```python
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from pulse import stats
from pulse.agents.digest import build_launch_input
from pulse.models import from_iso
from pulse.web import jobs
from pulse.web.cards import cards_by_ids
from pulse.web.charts import sentiment_chart
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters
from pulse.web.views.reports import require_agents

router = APIRouter()


def _pick(rows, name: str | None, today: str):
    for r in rows:
        if r["name"] == name:
            return r
    for r in rows:
        if r["date"] <= today:
            return r
    return rows[0]


@router.get("/launch", response_class=HTMLResponse)
def launch(request: Request, name: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    rows = conn.execute("SELECT * FROM launches ORDER BY date DESC, name").fetchall()
    if not rows:
        return render(request, "launch.html", conn, f, "launch", launches=[], sel=None)
    sel = _pick(rows, name, f.now.date().isoformat())
    try:
        data = build_launch_input(conn, sel, f.now)
    except LookupError:
        data = None
    chart, cards = None, []
    if data:
        start, end = from_iso(data["before"]["start"]), from_iso(data["after"]["end"])
        chart = sentiment_chart(stats.sentiment_series(conn, start, end), [(sel["date"], sel["name"])])
        cards = cards_by_ids(conn, [m["message_id"] for m in data["messages"][:12]])
    latest = conn.execute(
        "SELECT id, created_at FROM digests WHERE launch_id = ? ORDER BY created_at DESC, id DESC LIMIT 1", (sel["id"],)
    ).fetchone()
    return render(
        request, "launch.html", conn, f, "launch",
        launches=rows, sel=sel, data=data, chart=chart, cards=cards, latest=latest,
    )


@router.post("/launch/{launch_id}/digest")
def write_launch_digest(
    request: Request, launch_id: int, background: BackgroundTasks, conn=Depends(get_conn), f: Filters = Depends(get_filters)
):
    require_agents(request)
    row = conn.execute("SELECT * FROM launches WHERE id = ?", (launch_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such launch")
    if row["date"] > f.now.date().isoformat():
        raise HTTPException(status_code=400, detail="This launch hasn't happened yet")
    background.add_task(jobs.run_digest_job, request.app.state.settings, row["name"])
    return RedirectResponse(f"/reports{f.qs(pending=int(f.now.timestamp()))}", status_code=303)
```

In `pulse/web/app.py`, import `reports` and `launch` from `pulse.web.views` and add both to the router tuple.

- [ ] **Step 5: Create the templates**

Create `pulse/web/templates/reports.html`:

```html
{% extends "base.html" %}
{% block title %}Reports{% endblock %}
{% block main %}
<div class="head">
  <div><h1>Reports</h1><p class="sub">Digests and investigations. Citations link to the messages they used.</p></div>
  <form method="post" action="/reports/digest{{ f.qs() }}">
    {% if agents_on %}<button class="btn primary" type="submit">Write weekly digest</button>
    {% else %}<button class="btn" type="button" disabled title="{{ agents_off_text }}">Write weekly digest</button> <span class="pmeta">{{ agents_off_text }}</span>{% endif %}
  </form>
</div>
{% if job %}
  {% if job.state == 'running' %}
  <div id="report-list" hx-get="/reports{{ f.qs(pending=job.pending) }}" hx-trigger="every 5s" hx-select="#report-list" hx-swap="outerHTML">
    <p class="banner" role="status">Writing the digest. This page updates by itself.</p>
  {% elif job.state == 'done' %}
  <div id="report-list"><p class="banner" role="status">Digest ready.</p>
  {% else %}
  <div id="report-list"><p class="banner err" role="status">The digest failed: {{ job.reason }}.</p>
  {% endif %}
{% else %}
  <div id="report-list">
{% endif %}
  <section class="panel" style="margin-top:12px">
    {% for r in rows %}
    <div class="pain-row" style="grid-template-columns:minmax(0,1fr) auto">
      <div><a class="pname row-btn" href="{{ r.href }}{{ f.qs() }}">{{ r.title }}</a>
        <div class="pmeta">{{ 'Digest' if r.type == 'digest' else 'Investigation' }} · {{ r.created_at|utc }}</div></div>
      <span class="pill {{ 'ok' if r.state == 'done' else 'unanswered' if r.state == 'running' else 'frustrated' }}">{{ r.state }}</span>
    </div>
    {% else %}
    <p class="empty">No reports yet. Write a weekly digest, or ask Investigate a question from a pain point.</p>
    {% endfor %}
  </section>
</div>
{% endblock %}
```

Create `pulse/web/templates/report_digest.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card %}
{% block title %}{{ 'Launch digest' if d.kind == 'launch' else 'Weekly digest' }}{% endblock %}
{% block main %}
<div class="head">
  <div><h1>{% if d.kind == 'launch' %}Launch digest: {{ d.launch }}{% else %}Weekly digest{% endif %}</h1>
    <p class="sub">{{ d.period_start|utc }} to {{ d.period_end|utc }} · written {{ d.created_at|utc }}</p></div>
  <a class="btn" href="/reports{{ f.qs() }}">All reports</a>
</div>
<div class="grid2">
  <section class="panel"><div class="report">{{ html }}</div>
    {% if removed %}<p class="pmeta">{{ removed|length }} {{ 'citation' if removed|length == 1 else 'citations' }} removed because the agent was not shown {{ 'that message' if removed|length == 1 else 'those messages' }}.</p>{% endif %}
  </section>
  <section class="panel"><div class="panel-head"><h2>Messages cited</h2></div>
    <div class="msgs">{% for m in cited %}{{ message_card(m, now, f) }}{% else %}<p class="empty">No messages cited.</p>{% endfor %}</div>
  </section>
</div>
{% endblock %}
```

Create `pulse/web/templates/report_investigation.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card %}
{% block title %}Investigation{% endblock %}
{% block main %}
<div class="head">
  <div><h1>{{ inv.question }}</h1><p class="sub">Investigation · asked {{ inv.created_at|utc }}</p></div>
  <a class="btn" href="/reports{{ f.qs() }}">All reports</a>
</div>
{% if state == 'running' %}
<div id="inv-body" hx-get="/reports/investigation/{{ inv.id }}{{ f.qs() }}" hx-trigger="every 3s" hx-select="#inv-body" hx-swap="outerHTML">
  <p class="banner" role="status">Investigating. This usually takes under a minute; the page updates by itself.</p>
</div>
{% elif state == 'failed' %}
<div id="inv-body"><p class="banner err" role="status">{{ inv.markdown }}</p></div>
{% else %}
<div id="inv-body" class="grid2">
  <section class="panel"><div class="report">{{ html }}</div>
    {% if removed %}<p class="pmeta">{{ removed|length }} {{ 'citation' if removed|length == 1 else 'citations' }} removed because the agent was not shown {{ 'that message' if removed|length == 1 else 'those messages' }}.</p>{% endif %}
  </section>
  <section class="panel"><div class="panel-head"><h2>Messages cited</h2></div>
    <div class="msgs">{% for m in cited %}{{ message_card(m, now, f) }}{% else %}<p class="empty">No messages cited.</p>{% endfor %}</div>
  </section>
</div>
{% endif %}
{% endblock %}
```

Create `pulse/web/templates/launch.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card %}
{% block title %}Launch{% endblock %}
{% block main %}
<div class="head">
  <div><h1>{% if sel %}Launch: {{ sel.name }}{% else %}Launch{% endif %}</h1>
    <p class="sub">{% if sel %}{{ sel.date }} · equal windows before and after, up to 14 days · all channels{% else %}Compare the community before and after a release{% endif %}</p></div>
  {% if launches|length > 1 %}
  <div class="chips" role="group" aria-label="Launches">
    {% for l in launches %}<a class="chip" href="/launch{{ f.qs(name=l.name) }}"{% if sel and l.id == sel.id %} aria-current="true"{% endif %}>{{ l.name }}</a>{% endfor %}
  </div>
  {% endif %}
</div>
{% if not sel %}
<p class="empty">No launches yet. Add one under <code>[[launches]]</code> in pulse.toml with a name, date and keywords.</p>
{% elif not data %}
<p class="empty">{{ sel.name }} hasn't happened yet ({{ sel.date }}). Come back after launch day.</p>
{% else %}
<div class="compare">
  {% for label, side in (("Before", data.before), ("After", data.after)) %}
  <section class="panel">
    <span class="label">{{ label }} · {{ side.days }} days</span>
    <p class="big" style="margin:6px 0">{{ side.messages }} messages</p>
    <p class="pmeta">avg sentiment {{ side.avg_sentiment|signed }} · {{ side.negative }} negative · {{ side.needs_reply }} needed a reply</p>
  </section>
  {% endfor %}
</div>
<section class="panel">
  <div class="panel-head"><h2>Sentiment around the launch</h2>
    <form method="post" action="/launch/{{ sel.id }}/digest{{ f.qs() }}">
      {% if agents_on %}<button class="btn primary" type="submit">Write launch digest</button>
      {% else %}<button class="btn" type="button" disabled title="{{ agents_off_text }}">Write launch digest</button>{% endif %}
    </form>
  </div>
  <div class="chart-wrap">{{ chart }}</div>
  {% if latest %}<p class="pmeta">Latest launch digest: <a href="/reports/digest/{{ latest.id }}{{ f.qs() }}">{{ latest.created_at|utc }}</a></p>{% endif %}
</section>
<div class="grid2">
  <section class="panel"><div class="panel-head"><h2>Messages about the launch</h2><span class="note">keywords: {{ data.launch.keywords|join(', ') }}</span></div>
    <div class="msgs">{% for m in cards %}{{ message_card(m, now, f) }}{% else %}<p class="empty">No messages mention this launch yet.</p>{% endfor %}</div>
  </section>
  <section class="panel"><div class="panel-head"><h2>Top pain points since launch</h2></div>
    {% for t in data.top_themes_after %}
    <div class="pain-row" style="grid-template-columns:minmax(0,1fr) auto"><div><a class="pname row-btn" href="/pain{{ f.qs(theme=t.theme_id) }}">{{ t.name }}</a><div class="pmeta">{{ t.volume }} msgs</div></div><span class="score">{{ t.score|round|int }}</span></div>
    {% else %}<p class="empty">No pain points since launch.</p>{% endfor %}
  </section>
</div>
{% endif %}
{% endblock %}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_digest.py tests/test_web_reports_launch.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 7: Commit**

```bash
git add pulse/db.py pulse/agents/digest.py pulse/web tests/web_fakes.py tests/test_digest.py tests/test_web_reports_launch.py
git commit -m "feat: reports and launch views, digests written in the background, removed citations stored" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 10: Investigate from the dashboard

**Files:**
- Modify: `pulse/stats.py`, `pulse/agents/investigate.py`, `pulse/web/jobs.py`, `pulse/web/views/reports.py`, `pulse/web/app.py`, `pulse/web/templates/pain.html`, `pulse/web/templates/overview.html`, `pulse/web/templates/launch.html`, `pulse/web/templates/report_investigation.html`
- Create: `pulse/web/views/investigations.py`
- Test: `tests/test_stats.py` (append), `tests/test_investigate.py` (append), `tests/test_web_investigate.py`

**Interfaces:**
- Consumes: `run_investigation`, `Toolbox`, `stats.theme_member_ids`, `stats.theme_resolution`, `require_agents` (Task 9), `queries.JOB_TIMEOUT_MINUTES`.
- Produces:
  - `stats.theme_clause(conn, theme_id: int | None) -> tuple[str, list] | None` and keyword `theme_id: int | None = None` on `stats.period_summary` and `stats.sentiment_series` (an unknown theme gives an empty result).
  - Investigate's `query_stats` tool accepts optional `theme_id` (integer) and `channel_id` (string), as spec 6.4 lists; an unknown theme is a `ToolError`.
  - `run_investigation(conn, llm, question, now, *, context=None, investigation_id: int | None = None)`: with `investigation_id`, fills that existing row instead of inserting one (`LookupError` if it does not exist); always stores `removed_citations`.
  - `pulse.web.jobs.run_investigation_job(settings, inv_id: int, question: str, context: dict) -> None` (never leaves the row without markdown).
  - `POST /investigations` form fields `question` (1-500 characters), optional `theme_id`, optional `launch` → inserts the row with context `{theme_id?, launch?, channel_id?, days}` and redirects 303 to `/reports/investigation/{id}?...`; 400 for an empty question or non-numeric theme; 404 for an unknown theme or launch; 409 when agents are off.
  - A running investigation older than `JOB_TIMEOUT_MINUTES` shows as failed.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stats.py`:

```python
def test_period_summary_and_series_scope_by_theme():
    conn = _channel_db()
    with conn:
        conn.execute("INSERT INTO themes (id, name, created_at) VALUES (1, 'install', ?)", (to_iso(T0),))
        conn.execute("INSERT INTO themes (id, name, status, merged_into, created_at) VALUES (2, 'm1', 'merged', 1, ?)", (to_iso(T0),))
        conn.execute("INSERT INTO message_themes (message_id, theme_id) VALUES ('h1', 1), ('g1', 2)")
    s = stats.period_summary(conn, START, END, theme_id=1)
    assert s["messages"] == 2  # g1 is in a theme merged into 1
    assert stats.period_summary(conn, START, END, theme_id=1, channels=("200",))["messages"] == 1
    assert stats.period_summary(conn, START, END, theme_id=99)["messages"] == 0
    day = T0.replace(hour=0)
    assert stats.sentiment_series(conn, day, day + timedelta(days=1), theme_id=1)[0]["messages"] == 2
```

Append to `tests/test_investigate.py`:

```python
def test_run_investigation_fills_existing_row_and_stores_removed():
    from pulse.models import to_iso

    conn = seed()
    with conn:
        conn.execute("INSERT INTO investigations (id, question, context, created_at) VALUES (7, 'why?', '{}', ?)",
                     (to_iso(NOW),))
    backend = FakeBackend(steps=[StepResult("Because [[msg:ghost]].", (), 10, 5)])
    result = run_investigation(conn, make_llm(conn, make_config(), backend), "why?", NOW, investigation_id=7)
    assert result.investigation_id == 7
    row = conn.execute("SELECT * FROM investigations WHERE id = 7").fetchone()
    assert json.loads(row["removed_citations"]) == ["ghost"] and row["markdown"] == "Because ."
    assert conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 1
    with pytest.raises(LookupError):
        run_investigation(conn, make_llm(conn, make_config(), backend), "why?", NOW, investigation_id=99)


def test_query_stats_scopes_by_theme_and_channel():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("a", "x", minutes=0, channel_id="100"), msg("b", "y", minutes=1, channel_id="200")],
                    frozenset())
    set_triage(conn, "a", sentiment=-2)
    set_triage(conn, "b", sentiment=2)
    tid, _ = create_theme(conn, "install", "", T0)
    assign(conn, "a", tid)
    box = Toolbox(conn, T0 + timedelta(hours=1))
    by_theme = json.loads(box.execute(ToolCall("1", "query_stats", {"metric": "period_summary", "theme_id": tid})))
    assert by_theme["messages"] == 1 and by_theme["avg_sentiment"] == -2.0
    by_channel = json.loads(box.execute(ToolCall("2", "query_stats", {"metric": "period_summary", "channel_id": "200"})))
    assert by_channel["messages"] == 1 and by_channel["avg_sentiment"] == 2.0
    with pytest.raises(ToolError):
        box.execute(ToolCall("3", "query_stats", {"metric": "period_summary", "theme_id": 999}))
    with pytest.raises(ToolError):
        box.execute(ToolCall("4", "query_stats", {"metric": "period_summary", "channel_id": 5}))
```

Create `tests/test_web_investigate.py`:

```python
import json
from datetime import timedelta

from pulse.agents.base import ProviderError, StepResult
from pulse.db import connect
from pulse.models import to_iso
from tests.fakes import FakeBackend, make_llm
from tests.web_fakes import NOW, make_client


def factory_with(step_handler):
    backend = FakeBackend(step_handler=step_handler)
    return lambda conn, config: make_llm(conn, config, backend)


def answer(transcript, allow_tools):
    return StepResult("Mostly M1 installs.", (), 10, 5)


def test_start_investigation_from_pain_point(tmp_path):
    client = make_client(tmp_path, llm_factory=factory_with(answer))
    r = client.post("/investigations?days=14", data={"question": "Why are installs failing?", "theme_id": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/reports/investigation/1?days=14"
    page = client.get(r.headers["location"]).text
    assert "Mostly M1 installs." in page and "hx-trigger" not in page
    conn = connect(tmp_path / "pulse.db")
    assert json.loads(conn.execute("SELECT context FROM investigations WHERE id = 1").fetchone()[0]) == {
        "theme_id": 1, "days": 14,
    }


def test_failed_investigation_stops_polling(tmp_path):
    client = make_client(tmp_path, llm_factory=factory_with(lambda t, a: ProviderError("bad model")))
    page = client.post("/investigations", data={"question": "why?"}).text
    assert "Investigation failed" in page and "hx-trigger" not in page


def test_factory_error_marks_investigation_failed(tmp_path):
    def broken(conn, config):
        raise RuntimeError("no key")

    page = make_client(tmp_path, llm_factory=broken).post("/investigations", data={"question": "why?"}).text
    assert "Investigation failed: RuntimeError: no key" in page


def test_investigation_input_validation(tmp_path):
    client = make_client(tmp_path, llm_factory=factory_with(answer))
    assert client.post("/investigations", data={"question": "  "}).status_code == 400
    assert client.post("/investigations", data={"question": "x" * 501}).status_code == 400
    assert client.post("/investigations", data={"question": "why?", "theme_id": "abc"}).status_code == 400
    assert client.post("/investigations", data={"question": "why?", "theme_id": "999"}).status_code == 404
    assert client.post("/investigations", data={"question": "why?", "launch": "nope"}).status_code == 404
    (tmp_path / "off").mkdir()
    off = make_client(tmp_path / "off", demo=True, llm_factory=factory_with(answer))
    assert off.post("/investigations", data={"question": "why?"}).status_code == 409


def test_investigate_buttons_render(tmp_path):
    on = make_client(tmp_path, llm_factory=factory_with(answer))
    pain = on.get("/pain").text
    assert 'action="/investigations?days=14"' in pain and 'name="theme_id" value="1"' in pain
    assert "Ask why sentiment changed" in on.get("/").text
    assert "Ask about this launch" in on.get("/launch").text
    (tmp_path / "off").mkdir()
    off = make_client(tmp_path / "off").get("/pain").text
    assert "Agents are off" in off


def test_stale_running_investigation_shows_timeout(tmp_path):
    client = make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    with conn:
        conn.execute("INSERT INTO investigations (id, question, context, created_at) VALUES (5, 'why?', '{}', ?)",
                     (to_iso(NOW - timedelta(minutes=11)),))
    page = client.get("/reports/investigation/5").text
    assert "No result after 10 minutes" in page and "hx-trigger" not in page
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stats.py tests/test_investigate.py tests/test_web_investigate.py -q`
Expected: FAIL (`period_summary() got an unexpected keyword argument 'theme_id'`; `run_investigation() got an unexpected keyword argument 'investigation_id'`; 404 on `/investigations`).

- [ ] **Step 3: Implement the stats and agent changes**

In `pulse/stats.py`, add after `scope_clause`:

```python
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
```

In both `period_summary` and `sentiment_series`, add the keyword `theme_id: int | None = None` after `channels`, and right after `scope, scope_params = scope_clause(channels)` add:

```python
    theme = theme_clause(conn, theme_id)
    theme_sql, theme_params = theme if theme is not None else (" AND 0", [])
```

then append `theme_sql` to the SQL right after `{scope}` (for `sentiment_series`, before ` GROUP BY day`) and `*theme_params` to the parameters right after `*scope_params`. For example, the `period_summary` query becomes:

```python
    rows = conn.execute(
        "SELECT t.sentiment, t.kind, t.needs_reply FROM messages m JOIN triage t ON t.message_id = m.id"
        f" WHERE {_COMMUNITY} AND m.created_at >= ? AND m.created_at < ?{scope}{theme_sql}",
        (to_iso(start), to_iso(end), *scope_params, *theme_params),
    ).fetchall()
```

In `pulse/agents/investigate.py`:

1. In the `query_stats` `ToolSpec`, extend the description's first sentence to "Aggregate statistics for a date range [start, end), optionally for one pain point (theme_id) or one channel and its threads (channel_id)." and add these properties next to `start` and `end`:

```python
            "theme_id": {"type": "integer"},
            "channel_id": {"type": "string"},
```

2. Replace `Toolbox.query_stats` with:

```python
    def query_stats(self, args: dict):
        metric = args.get("metric")
        start, end = self._range(args)
        theme_id = self._int(args, "theme_id")
        if theme_id is not None and not stats.theme_member_ids(self.conn, theme_id):
            raise ToolError(f"no theme {theme_id}")
        channel_id = args.get("channel_id")
        if channel_id is not None and (not isinstance(channel_id, str) or not channel_id):
            raise ToolError("channel_id must be a non-empty string")
        channels = (channel_id,) if channel_id else None
        if metric == "period_summary":
            return stats.period_summary(self.conn, start, end, channels=channels, theme_id=theme_id)
        if metric == "sentiment_series":
            return stats.sentiment_series(self.conn, start, end, channels=channels, theme_id=theme_id)
        if metric == "theme_scores":
            window = max(1, (end - start).days)
            return [asdict(s) for s in stats.theme_scores(self.conn, end, window_days=window, channels=channels)]
        if metric == "queue_counts":
            return stats.queue_counts(self.conn)
        raise ToolError(f"unknown metric {metric!r}")
```

3. Change `run_investigation`'s signature to add `investigation_id: int | None = None` after `context`, replace its `with conn: inv_id = ...` insert block with:

```python
    if investigation_id is None:
        with conn:
            inv_id = int(conn.execute(
                "INSERT INTO investigations (question, context, created_at) VALUES (?, ?, ?)",
                (question, json.dumps(context), to_iso(now)),
            ).lastrowid)
    else:
        if conn.execute("SELECT 1 FROM investigations WHERE id = ?", (investigation_id,)).fetchone() is None:
            raise LookupError(f"no investigation {investigation_id}")
        inv_id = investigation_id
```

and replace the final `UPDATE` with:

```python
    with conn:
        conn.execute(
            "UPDATE investigations SET markdown = ?, cited_message_ids = ?, removed_citations = ?, run_id = ?"
            " WHERE id = ?",
            (markdown, json.dumps(cited), json.dumps(removed), resp.run_id, inv_id),
        )
```

- [ ] **Step 4: Implement the web side**

Add to `pulse/web/jobs.py` (add `from pulse.agents.investigate import run_investigation` to the imports):

```python
def run_investigation_job(settings: WebSettings, inv_id: int, question: str, context: dict) -> None:
    conn = connect(settings.db_path)
    try:
        llm = settings.llm_factory(conn, settings.config)
        run_investigation(conn, llm, question, settings.clock(), context=context, investigation_id=inv_id)
    except Exception as e:  # the row must never be left running
        log.warning("investigation %s failed: %s", inv_id, e)
        with conn:
            conn.execute(
                "UPDATE investigations SET markdown = ? WHERE id = ? AND markdown IS NULL",
                (f"Investigation failed: {type(e).__name__}: {e}", inv_id),
            )
    finally:
        conn.close()
```

Create `pulse/web/views/investigations.py`:

```python
import json

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from pulse import stats
from pulse.models import to_iso
from pulse.web import jobs
from pulse.web.deps import get_conn, get_filters
from pulse.web.filters import Filters
from pulse.web.views.reports import require_agents

router = APIRouter()
QUESTION_MAX = 500


@router.post("/investigations")
def start_investigation(
    request: Request,
    background: BackgroundTasks,
    question: str = Form(""),
    theme_id: str = Form(""),
    launch: str = Form(""),
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    require_agents(request)
    question = question.strip()
    if not question or len(question) > QUESTION_MAX:
        raise HTTPException(status_code=400, detail=f"Ask a question of 1 to {QUESTION_MAX} characters.")
    context: dict = {}
    if theme_id:
        try:
            tid = int(theme_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="theme_id must be a number") from e
        if tid not in stats.theme_resolution(conn):
            raise HTTPException(status_code=404, detail=f"No theme {tid}")
        context["theme_id"] = tid
    if launch:
        if conn.execute("SELECT 1 FROM launches WHERE name = ?", (launch,)).fetchone() is None:
            raise HTTPException(status_code=404, detail=f"No launch {launch!r}")
        context["launch"] = launch
    if f.channel:
        context["channel_id"] = f.channel
    context["days"] = f.days
    with conn:
        inv_id = int(conn.execute(
            "INSERT INTO investigations (question, context, created_at) VALUES (?, ?, ?)",
            (question, json.dumps(context), to_iso(f.now)),
        ).lastrowid)
    background.add_task(jobs.run_investigation_job, request.app.state.settings, inv_id, question, context)
    return RedirectResponse(f"/reports/investigation/{inv_id}{f.qs()}", status_code=303)
```

In `pulse/web/app.py`, import `investigations` from `pulse.web.views` and add it to the router tuple.

In `pulse/web/views/reports.py`, add `from pulse.models import from_iso` and replace `investigation_detail` with:

```python
@router.get("/reports/investigation/{inv_id}", response_class=HTMLResponse)
def investigation_detail(request: Request, inv_id: int, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    row = conn.execute("SELECT * FROM investigations WHERE id = ?", (inv_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="No such investigation")
    md = row["markdown"]
    failure = None
    if md is None:
        state = "running"
        if (f.now - from_iso(row["created_at"])).total_seconds() > queries.JOB_TIMEOUT_MINUTES * 60:
            state, failure = "failed", f"No result after {queries.JOB_TIMEOUT_MINUTES} minutes. Try asking again."
    elif md.startswith("Investigation failed"):
        state, failure = "failed", md
    else:
        state = "done"
    return render(
        request, "report_investigation.html", conn, f, "reports",
        inv=row, state=state, failure=failure,
        html=Markup(render_html(md, conn)) if state == "done" else None,
        cited=_cards(conn, row["cited_message_ids"]) if state == "done" else [],
        removed=json.loads(row["removed_citations"] or "[]"),
    )
```

In `pulse/web/templates/report_investigation.html`, change the failed branch to show `{{ failure }}` instead of `{{ inv.markdown }}`:

```html
<div id="inv-body"><p class="banner err" role="status">{{ failure }}</p></div>
```

In `pulse/web/templates/pain.html`, replace `{% block ask_why %}{% endblock %}` with:

```html
    <div class="panel-head" style="margin-top:18px"><h2>Ask why</h2></div>
    <form class="evidence" method="post" action="/investigations{{ f.qs() }}">
      <p class="mut" style="margin:0">Investigate reads stats and messages with read-only tools and answers in a few sentences. Every claim links to the messages it used.</p>
      <input type="hidden" name="theme_id" value="{{ sel.id }}">
      <textarea id="inv-q" name="question" maxlength="500" aria-label="Question">Why is "{{ sel.name }}" a pain point right now, and what would fix it?</textarea>
      {% if agents_on %}<button class="btn primary" type="submit" style="align-self:flex-start">Investigate</button>
      {% else %}<button class="btn" type="button" disabled style="align-self:flex-start">Investigate</button><span class="pmeta">{{ agents_off_text }}</span>{% endif %}
    </form>
```

In `pulse/web/templates/overview.html`, right after the `<div class="legend">...</div>` line inside the "Sentiment and volume" panel, add:

```html
      <form class="form-row" method="post" action="/investigations{{ f.qs() }}" style="margin-top:12px">
        <input type="hidden" name="question" value="What changed in community sentiment over the last {{ f.days }} days, and why?">
        {% if agents_on %}<button class="btn" type="submit">Ask why sentiment changed</button>
        {% else %}<button class="btn" type="button" disabled title="{{ agents_off_text }}">Ask why sentiment changed</button>{% endif %}
      </form>
```

In `pulse/web/templates/launch.html`, inside the "Sentiment around the launch" panel, right after the `{% if latest %}...{% endif %}` line, add:

```html
  <form class="form-row" method="post" action="/investigations{{ f.qs() }}" style="margin-top:12px">
    <input type="hidden" name="launch" value="{{ sel.name }}">
    <input type="hidden" name="question" value="How did {{ sel.name }} land, and what are people stuck on since launch?">
    {% if agents_on %}<button class="btn" type="submit">Ask about this launch</button>
    {% else %}<button class="btn" type="button" disabled title="{{ agents_off_text }}">Ask about this launch</button>{% endif %}
  </form>
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stats.py tests/test_investigate.py tests/test_web_investigate.py tests/test_web_reports_launch.py tests/test_web_pain.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 6: Commit**

```bash
git add pulse/stats.py pulse/agents/investigate.py pulse/web tests/test_stats.py tests/test_investigate.py tests/test_web_investigate.py
git commit -m "feat: start Investigate from the dashboard; query_stats by theme and channel" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 11: Runs view

**Files:**
- Modify: `pulse/web/queries.py`, `pulse/web/app.py`
- Create: `pulse/web/views/runs.py`, `pulse/web/templates/runs.html`
- Test: `tests/test_web_runs.py`

**Interfaces:**
- Consumes: `context.spend_today`, `queries.day_keys`, `render`.
- Produces:
  - `queries.daily_costs(conn, now, days=14) -> list[dict]` keys `day, runs, cost, failed` (one row per UTC day, oldest first, zero-filled).
  - `queries.recent_runs(conn, *, status=None, limit=100) -> list[sqlite3.Row]` (newest first; `status` in `ok | failed | skipped_budget`, others ignored).
  - `GET /runs?status=` showing today's spend against the cap, cost per day, and recent runs with error text.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_runs.py`:

```python
from tests.web_fakes import make_client


def test_runs_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/runs")
    assert r.status_code == 200 and "No agent runs yet" in r.text and "$0.00" in r.text


def test_runs_show_spend_daily_costs_and_errors(tmp_path):
    html = make_client(tmp_path).get("/runs").text
    assert "$0.04" in html and "of $5.00" in html
    assert "2026-09-30" in html  # today's row in the daily table
    assert "provider 500" in html and "triage" in html and "digest" in html


def test_runs_status_filter(tmp_path):
    client = make_client(tmp_path)
    html = client.get("/runs?status=failed").text
    assert "provider 500" in html and "anthropic:m-triage" not in html
    assert client.get("/runs?status=bogus").status_code == 200
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_runs.py -q`
Expected: FAIL with 404 on `/runs`.

- [ ] **Step 3: Implement**

Add to `pulse/web/queries.py`:

```python
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
```

Create `pulse/web/views/runs.py`:

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse.web import queries
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


@router.get("/runs", response_class=HTMLResponse)
def runs(request: Request, status: str | None = None, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    status = status if status in queries.RUN_STATUSES else None
    return render(
        request, "runs.html", conn, f, "runs",
        days=queries.daily_costs(conn, f.now), runs=queries.recent_runs(conn, status=status), status=status,
    )
```

In `pulse/web/app.py`, import `runs` from `pulse.web.views` and add it to the router tuple.

Create `pulse/web/templates/runs.html`:

```html
{% extends "base.html" %}
{% block title %}Runs{% endblock %}
{% block main %}
<div class="head">
  <div><h1>Runs</h1><p class="sub">Every model call the agents made: cost, tokens and failures. The daily cap resets at midnight UTC.</p></div>
  <div class="chips" role="group" aria-label="Filter runs">
    <a class="chip" href="/runs{{ f.qs() }}"{% if not status %} aria-current="true"{% endif %}>All</a>
    <a class="chip" href="/runs{{ f.qs(status='failed') }}"{% if status == 'failed' %} aria-current="true"{% endif %}>Failed</a>
    <a class="chip" href="/runs{{ f.qs(status='skipped_budget') }}"{% if status == 'skipped_budget' %} aria-current="true"{% endif %}>Skipped by cap</a>
  </div>
</div>
<div class="grid2">
  <section class="panel">
    <div class="panel-head"><h2>Spend today</h2></div>
    <p class="big" style="margin:0">${{ "%.2f"|format(spent) }} <span class="pmeta">of ${{ "%.2f"|format(cap) }}</span></p>
    {% set pct = ((spent / cap * 100) if cap > 0 else 0)|round|int %}
    <div class="share" style="margin-top:10px"><div class="track" style="max-width:none"><div class="fill" style="width:{{ [pct, 100]|min }}%;background:{{ 'var(--neg)' if over_cap else 'var(--accent)' }}"></div></div>{{ pct }}%</div>
  </section>
  <section class="panel">
    <div class="panel-head"><h2>Cost per day</h2><span class="note">last 14 days, UTC</span></div>
    <div class="tbl-wrap"><table class="chan">
      <thead><tr><th>Day</th><th class="num">Runs</th><th class="num">Failed</th><th class="num">Cost</th></tr></thead>
      <tbody>{% for d in days|reverse %}<tr><td class="where">{{ d.day }}</td><td class="num">{{ d.runs }}</td><td class="num {{ 'neg' if d.failed else 'mut' }}">{{ d.failed }}</td><td class="num">${{ "%.2f"|format(d.cost) }}</td></tr>{% endfor %}</tbody>
    </table></div>
  </section>
</div>
<section class="panel">
  <div class="panel-head"><h2>Recent runs</h2></div>
  {% if runs %}
  <div class="tbl-wrap"><table>
    <thead><tr><th>Started</th><th>Agent</th><th>Model</th><th>Status</th><th class="num">Tokens in / out</th><th class="num">Cost</th><th>Error</th></tr></thead>
    <tbody>
    {% for r in runs %}
      <tr>
        <td>{{ r.started_at|utc }}</td>
        <td>{{ r.agent }}</td>
        <td class="where" style="font-family:var(--mono)">{{ r.model }}</td>
        <td><span class="pill {{ 'ok' if r.status == 'ok' else 'unanswered' if r.status == 'skipped_budget' else 'frustrated' }}">{{ r.status }}</span></td>
        <td class="num">{{ r.input_tokens }} / {{ r.output_tokens }}</td>
        <td class="num">${{ "%.4f"|format(r.cost_usd) }}</td>
        <td class="neg">{{ r.error or '' }}</td>
      </tr>
    {% endfor %}
    </tbody>
  </table></div>
  {% else %}<p class="empty">No agent runs yet{% if status %} with this status{% endif %}.</p>{% endif %}
</section>
{% endblock %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_runs.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 5: Commit**

```bash
git add pulse/web tests/test_web_runs.py
git commit -m "feat: runs view with spend against the cap, cost per day and failures" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---
### Task 12: Demo dataset, `seed-demo` and the `web` command

**Files:**
- Create: `pulse/demo.py`
- Modify: `pulse/run.py`
- Test: `tests/test_demo.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: everything above; `upsert_messages`, `sync_launches`, `refresh_mod_queue`, `theme_status.set_status`, `citations.cited_ids`, `create_app`, `WebSettings`, `build_llm`.
- Produces:
  - `pulse.demo`: `SERVER_NAME = "Acme SDK Community (demo)"`, `demo_config(db_path, now) -> Config` (no keys needed), `is_demo_db(path) -> bool`, `seed_demo(path, now) -> dict` (keys `messages, themes, open_queue, digests, investigations, runs`; deterministic for a given `now`; raises `FileExistsError` instead of overwriting a database that `seed_demo` did not create; marks its databases with a `demo_marker` table).
  - CLI: `python -m pulse.run seed-demo [--db demo.db]`; `python -m pulse.run web [--host 127.0.0.1] [--port 8321] [--db PATH] [--name NAME] [--demo]`. `web --demo` needs no `pulse.toml` and no keys; `web` without `--demo` loads `pulse.toml` and turns agents on with `build_llm`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_demo.py`:

```python
import pytest
from fastapi.testclient import TestClient

from pulse.db import connect
from pulse.demo import demo_config, is_demo_db, seed_demo
from pulse.web.app import create_app
from pulse.web.settings import WebSettings
from tests.web_fakes import NOW


def test_seed_demo_counts(tmp_path):
    counts = seed_demo(tmp_path / "demo.db", NOW)
    assert counts["messages"] > 400
    assert counts["themes"] == 8
    assert counts["open_queue"] > 0
    assert counts["digests"] == 1 and counts["investigations"] == 1
    assert counts["runs"] > 20
    assert is_demo_db(tmp_path / "demo.db")


def test_seed_demo_is_deterministic(tmp_path):
    a = seed_demo(tmp_path / "a.db", NOW)
    b = seed_demo(tmp_path / "b.db", NOW)
    assert a == b
    first = lambda p: connect(p).execute("SELECT content FROM messages ORDER BY created_at, id LIMIT 1").fetchone()[0]
    assert first(tmp_path / "a.db") == first(tmp_path / "b.db")


def test_seed_demo_refuses_to_overwrite_real_db_but_replaces_demo(tmp_path):
    real = tmp_path / "pulse.db"
    connect(real).close()
    with pytest.raises(FileExistsError):
        seed_demo(real, NOW)
    demo = tmp_path / "demo.db"
    seed_demo(demo, NOW)
    assert seed_demo(demo, NOW)["themes"] == 8  # a previous demo is replaced


def test_demo_dashboard_pages_render(tmp_path):
    db = tmp_path / "demo.db"
    seed_demo(db, NOW)
    app = create_app(WebSettings(db_path=db, config=demo_config(db, NOW), server_name="Acme SDK Community (demo)",
                                 demo=True, clock=lambda: NOW))
    client = TestClient(app)
    for path in ("/", "/pain", "/bugs", "/queue", "/messages", "/launch", "/reports", "/runs",
                 "/reports/digest/1", "/reports/investigation/1", "/?days=90", "/pain?days=7"):
        assert client.get(path).status_code == 200, path
    pain = client.get("/pain").text
    assert "Fix shipped" in pain and "Fix in progress" in pain and "Acknowledged" in pain
    assert "Agents are off in demo mode" in client.get("/reports").text
    assert "M1 install fails on v2" in client.get("/messages?q=wheel").text
```

Append to `tests/test_cli.py`:

```python
def _capture_serve(monkeypatch):
    served = {}
    monkeypatch.setattr("pulse.run.uvicorn.run",
                        lambda app, host, port, **kw: served.update(app=app, host=host, port=port))
    return served


def test_seed_demo_then_web_demo(tmp_path, monkeypatch, capsys):
    db = tmp_path / "demo.db"
    assert main(["seed-demo", "--db", str(db)]) == 0
    assert "web --demo" in capsys.readouterr().out
    served = _capture_serve(monkeypatch)
    assert main(["web", "--demo", "--db", str(db), "--port", "9000"]) == 0
    assert served["port"] == 9000 and served["host"] == "127.0.0.1"
    settings = served["app"].state.settings
    assert settings.demo and not settings.agents_on and settings.db_path == db


def test_web_demo_without_db_exits_2(tmp_path, capsys):
    assert main(["web", "--demo", "--db", str(tmp_path / "missing.db")]) == 2
    assert "seed-demo" in capsys.readouterr().err


def test_seed_demo_refuses_real_db(tmp_path, capsys):
    real = tmp_path / "pulse.db"
    connect(real).close()
    assert main(["seed-demo", "--db", str(real)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_web_with_config_turns_agents_on(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    served = _capture_serve(monkeypatch)
    assert main(["--config", str(tmp_path / "pulse.toml"), "web", "--name", "Acme"]) == 0
    settings = served["app"].state.settings
    assert settings.agents_on and not settings.demo and settings.server_name == "Acme"
    assert settings.db_path.name == "pulse.db"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_demo.py tests/test_cli.py -q`
Expected: FAIL (`No module named 'pulse.demo'`; `invalid choice: 'seed-demo'`).

- [ ] **Step 3: Implement `pulse/demo.py`**

```python
"""A synthetic DevRel community for demos and pitches (spec 9, 15.5).

Deterministic for a given `now`, needs no API keys, and never touches real data:
seed_demo refuses to overwrite a database it did not create.
"""
from __future__ import annotations

import json
import random
import sqlite3
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

from pulse.citations import cited_ids
from pulse.config import AGENTS, Config, Launch, ModelRef, Price
from pulse.db import connect
from pulse.models import Message, to_iso
from pulse.modqueue import refresh_mod_queue
from pulse.store import sync_launches, upsert_messages
from pulse.theme_status import set_status

SERVER_NAME = "Acme SDK Community (demo)"
GUILD = "900000000000000001"
HELP, GENERAL, FEEDBACK, ANNOUNCE = (
    "100000000000000001", "200000000000000001", "400000000000000001", "500000000000000001",
)
M1_THREAD = "110000000000000777"
CHANNEL_NAMES = {
    HELP: "help", GENERAL: "general", FEEDBACK: "feedback", ANNOUNCE: "announcements",
    M1_THREAD: "M1 install fails on v2",
}
TEAM = {"t1": "sam", "t2": "priya"}
USERS = (
    "alice", "bob", "carol", "dan", "erin", "fox", "gus", "hal", "ivy", "jo", "kai", "lena", "mo",
    "nia", "omar", "pia", "quinn", "ravi", "sol", "tess", "uma", "vik", "wes", "xin", "yara", "zed",
)
DAYS = 30
LAUNCH_DAYS_AGO = 5
SHIPPED_DAYS_AGO = 2

THEMES = [
    {"key": "auth", "name": "Auth migration and token exchange docs", "topic": "auth docs",
     "description": "The v2 auth guide skips the token exchange step and v1 tokens stopped working.",
     "kind": "docs", "channel": HELP, "sentiment": (-2, 0), "before": 1, "after": 7,
     "status": ("acknowledged", "Docs team rewriting the token exchange section"),
     "phrases": (
         "How do I migrate acme.login() to the new auth flow? The guide skips the token exchange step",
         "the auth quickstart throws InvalidGrant when copied verbatim",
         "docs mention exchange_token() but it doesn't exist in 2.0",
         "v1 tokens stopped working after the upgrade and the changelog didn't say so",
         "is there a v1 -> v2 auth migration example anywhere?",
         "same question about the auth guide, still confused",
     )},
    {"key": "wheels", "name": "Install fails on M1 / Python 3.13 (arm64 wheels)", "topic": "arm64 wheel",
     "description": "No arm64 or Python 3.13 wheels for 2.0, so installs fail on Apple silicon.",
     "kind": "bug", "channel": HELP, "sentiment": (-2, -1), "before": 1, "after": 5,
     "status": ("in_progress", "arm64 and 3.13 wheels building for 2.0.2"),
     "phrases": (
         "pip install acme fails on M1: no matching wheel for arm64",
         "still getting the arm64 wheel error on 2.0.1 via poetry",
         "no wheels for python 3.13 yet?",
         "M1 install broke after upgrading to v2",
         "had to pin 1.9 because 2.0 won't install on my mac",
     )},
    {"key": "builds", "name": "Builds failing while status page is green", "topic": "builds",
     "description": "CI builds fail at the deploy step while the status page reports no incident.",
     "kind": "bug", "channel": GENERAL, "sentiment": (-2, -1), "before": 0, "after": 3, "status": None,
     "phrases": (
         "the status page says green but our builds are failing",
         "CI broken again, third day in a row",
         "builds time out at the deploy step since this morning",
         "is there an incident? every build fails",
     )},
    {"key": "charts", "name": "Dashboard charts blank", "topic": "dashboard charts",
     "description": "Usage charts rendered empty after the v2 dashboard update.",
     "kind": "bug", "channel": GENERAL, "sentiment": (-1, -1), "before": 0, "after": 3,
     "status": ("shipped", "Fix shipped in the dashboard deploy"),
     "phrases": (
         "dashboard charts are blank for me since yesterday",
         "usage graphs show nothing after the update",
         "the metrics page loads but every chart is empty",
     )},
    {"key": "ratelimit", "name": "429s earlier than v1 (rate limits)", "topic": "rate limits",
     "description": "Requests hit 429 Too Many Requests sooner than they did on v1.",
     "kind": "bug", "channel": HELP, "sentiment": (-1, 0), "before": 1, "after": 2, "status": None,
     "phrases": (
         "getting 429 Too Many Requests way earlier than v1 did",
         "did the rate limits change in 2.0?",
         "retry config doesn't seem to apply, still 429s",
     )},
    {"key": "wsl", "name": "acme deploy hangs on WSL", "topic": "wsl deploy",
     "description": "acme deploy stalls at the upload step on Windows Subsystem for Linux.",
     "kind": "bug", "channel": HELP, "sentiment": (-1, -1), "before": 1, "after": 1, "status": None,
     "phrases": (
         "acme deploy sits at 'uploading' forever on WSL",
         "deploy hangs on windows/WSL2, works fine on mac",
     )},
    {"key": "docsite", "name": "Docs site 404s and broken links", "topic": "docs site",
     "description": "Reference pages 404 and quickstart links point to moved pages.",
     "kind": "docs", "channel": FEEDBACK, "sentiment": (-1, -1), "before": 1, "after": 1, "status": None,
     "phrases": (
         "docs site is 404ing on the python reference",
         "broken link in the quickstart to the CLI page",
     )},
    {"key": "coldstart", "name": "Cold start latency", "topic": "cold start",
     "description": "Functions take around three seconds to start cold.",
     "kind": "feature_request", "channel": FEEDBACK, "sentiment": (-1, 0), "before": 1, "after": 1,
     "status": None,
     "phrases": (
         "cold starts are around 3s for us, any plans to improve?",
         "would love a warm pool option for functions",
     )},
]

OTHER = [
    {"kind": "praise", "channel": GENERAL, "sentiment": (1, 2), "weight": (3, 4), "phrases": (
        "honestly v2 is the best release you've shipped",
        "v2 builds cut our CI from 12 min to 4",
        "the new dashboard is fast, really nice work",
        "typescript sdk types are so much better now",
        "great talk at the conference yesterday",
    )},
    {"kind": "feature_request", "channel": FEEDBACK, "sentiment": (0, 0), "weight": (1, 1), "phrases": (
        "would love a Django integration guide",
        "any plans for audit log export?",
        "an OpenTelemetry exporter would help us a lot",
    )},
    {"kind": "other", "channel": GENERAL, "sentiment": (0, 0), "weight": (4, 3), "phrases": (
        "anyone going to the Berlin meetup?",
        "is the conference talk recorded?",
        "good morning everyone",
        "anyone here using acme with django?",
    )},
]

STAFF_REPLIES = (
    "Thanks for flagging, we're looking into it.",
    "Fixed in 2.0.1, can you upgrade and try again?",
    "Here's the updated guide section, let us know if it helps.",
    "We're tracking this and will post an update in this channel.",
)


def demo_config(db_path, now: datetime) -> Config:
    launch_day = (now - timedelta(days=LAUNCH_DAYS_AGO)).date().isoformat()
    return Config(
        guild_id=GUILD,
        channel_ids=(),
        team_member_ids=frozenset(TEAM),
        reply_window_hours=12.0,
        frustration_threshold=-2,
        models={a: ModelRef("anthropic", "demo") for a in AGENTS},
        daily_usd_cap=5.0,
        pricing={"anthropic:demo": Price(1.0, 5.0)},
        launches=(Launch("v2.0", launch_day, ("v2", "install", "auth")),),
        db_path=Path(db_path),
        imports_dir=Path("imports"),
    )


def is_demo_db(path) -> bool:
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'demo_marker'"
        ).fetchone() is not None
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def seed_demo(path, now: datetime) -> dict:
    path = Path(path)
    if path.exists():
        if not is_demo_db(path):
            raise FileExistsError(f"{path} exists and is not a demo database; refusing to overwrite it")
        for suffix in ("", "-wal", "-shm"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)
    rng = random.Random(42)
    config = demo_config(path, now)
    today = datetime.combine(now.date(), time.min, timezone.utc)
    launch = today - timedelta(days=LAUNCH_DAYS_AGO)
    shipped = today - timedelta(days=SHIPPED_DAYS_AGO)

    messages: list[Message] = []
    labels: dict[str, dict] = {}
    members: dict[str, list[str]] = {}
    next_id = iter(range(1_300_000_000_000_000_000, 1_400_000_000_000_000_000))

    def add(channel, author_id, author, content, at, *, thread=None, parent=None, reply_to=None, label=None, theme=None):
        mid = str(next(next_id))
        messages.append(Message(
            id=mid, guild_id=GUILD, channel_id=channel, author_id=author_id, author_name=author, content=content,
            created_at=at, channel_name=CHANNEL_NAMES[channel], thread_id=thread, reply_to_id=reply_to,
            source="demo", parent_channel_id=parent,
        ))
        if label is not None:
            labels[mid] = label
        if theme is not None:
            members.setdefault(theme, []).append(mid)
        return mid

    add(ANNOUNCE, "t1", TEAM["t1"],
        "v2.0 is out: a new auth flow, faster builds and a rebuilt dashboard. Upgrade notes are in the changelog.",
        launch + timedelta(hours=9))
    for offset in range(DAYS, -1, -1):
        day = today - timedelta(days=offset)
        after = day >= launch
        volume = rng.randint(10, 16) if not after else max(14, 48 - 6 * (day - launch).days + rng.randint(-4, 4))
        for _ in range(volume):
            at = day + timedelta(minutes=rng.randint(0, 24 * 60 - 1))
            if at >= now:
                continue
            pool = [(t, 0 if t["key"] == "charts" and day >= shipped else (t["after"] if after else t["before"]))
                    for t in THEMES]
            pool += [(o, o["weight"][1] if after else o["weight"][0]) for o in OTHER]
            spec = rng.choices([p for p, _ in pool], weights=[w for _, w in pool])[0]
            author = rng.choice(USERS)
            channel, thread, parent = spec["channel"], None, None
            if spec.get("key") == "wheels" and after:
                channel, thread, parent = M1_THREAD, M1_THREAD, HELP
            needs = spec["kind"] in ("bug", "docs", "question") and rng.random() < 0.8
            label = {
                "sentiment": rng.randint(*spec["sentiment"]), "kind": spec["kind"], "needs_reply": needs,
                "topics": [spec["topic"]] if "topic" in spec else [],
            }
            mid = add(channel, f"u-{author}", author, rng.choice(spec["phrases"]), at,
                      thread=thread, parent=parent, label=label, theme=spec.get("key"))
            if needs and rng.random() < 0.45:
                reply_at = at + timedelta(minutes=rng.randint(10, 600))
                if reply_at < now:
                    staff = rng.choice(sorted(TEAM))
                    add(channel, staff, TEAM[staff], rng.choice(STAFF_REPLIES), reply_at,
                        thread=thread, parent=parent, reply_to=None if thread else mid)

    conn = connect(path)
    try:
        with conn:
            conn.execute("CREATE TABLE demo_marker (created_at TEXT NOT NULL)")
            conn.execute("INSERT INTO demo_marker VALUES (?)", (to_iso(now),))
        upsert_messages(conn, messages, config.team_member_ids)
        with conn:
            conn.executemany(
                "INSERT INTO triage (message_id, sentiment, confidence, kind, topics, needs_reply, prompt_version,"
                " created_at, labeler, themed_at) VALUES (?, ?, 0.9, ?, ?, ?, 'demo', ?, 'llm', ?)",
                [(mid, l["sentiment"], l["kind"], json.dumps(l["topics"]), int(l["needs_reply"]), to_iso(now), to_iso(now))
                 for mid, l in labels.items()],
            )
            created = {m.id: m.created_at for m in messages}
            theme_ids: dict[str, int] = {}
            for t in THEMES:
                ids = members.get(t["key"], [])
                first = min((created[i] for i in ids), default=now)
                tid = int(conn.execute(
                    "INSERT INTO themes (name, description, created_at) VALUES (?, ?, ?)",
                    (t["name"], t["description"], to_iso(first)),
                ).lastrowid)
                theme_ids[t["key"]] = tid
                conn.executemany("INSERT INTO message_themes (message_id, theme_id) VALUES (?, ?)", [(i, tid) for i in ids])
        for t in THEMES:
            if t["status"]:
                status, note = t["status"]
                set_status(conn, theme_ids[t["key"]], status, note, shipped if status == "shipped" else now - timedelta(days=1))
        sync_launches(conn, config.launches)
        refresh_mod_queue(conn, config, now)
        _reports_and_runs(conn, now, labels, members, rng)
        return {
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "themes": conn.execute("SELECT COUNT(*) FROM themes").fetchone()[0],
            "open_queue": conn.execute("SELECT COUNT(*) FROM mod_queue WHERE status = 'open'").fetchone()[0],
            "digests": conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0],
            "investigations": conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0],
            "runs": conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0],
        }
    finally:
        conn.close()


def _reports_and_runs(conn, now: datetime, labels: dict, members: dict, rng: random.Random) -> None:
    praise = [mid for mid, l in labels.items() if l["kind"] == "praise"]
    auth, wheels = members["auth"], members["wheels"]
    oldest = conn.execute(
        "SELECT message_id FROM mod_queue WHERE status = 'open' ORDER BY opened_at, id LIMIT 1"
    ).fetchone()
    waiting = conn.execute("SELECT COUNT(*) FROM mod_queue WHERE status = 'open'").fetchone()[0]
    digest = (
        "## What's landing well\n"
        f"Build speed and the new dashboard get the most praise [[msg:{praise[-1]}]] [[msg:{praise[-2]}]].\n\n"
        "## Top pain points\n"
        f"- Auth migration docs are the biggest source of frustration since v2.0; the token exchange step is missing [[msg:{auth[-1]}]] [[msg:{auth[-2]}]].\n"
        f"- M1 and Python 3.13 installs fail without arm64 wheels [[msg:{wheels[-1]}]]. A fix is building for 2.0.2.\n"
        "- Dashboard chart complaints stopped after the fix shipped two days ago.\n\n"
        "## Needs attention\n"
        f"{waiting} messages are waiting for a staff reply"
        + (f"; the oldest is [[msg:{oldest['message_id']}]]" if oldest else "") + ".\n\n"
        "## Suggested priorities\n"
        "1. Publish the token exchange section of the auth guide.\n"
        "2. Ship arm64 and 3.13 wheels in 2.0.2 and reply in the M1 thread.\n"
        "3. Post an incident note when builds fail while the status page is green.\n"
    )
    investigation = (
        f"Sentiment fell right after v2.0. Most of the drop is auth migration confusion [[msg:{auth[0]}]] "
        f"and failed installs on Apple silicon [[msg:{wheels[0]}]]. Praise for build speed held up [[msg:{praise[-1]}]], "
        "so the release itself landed; the docs and packaging did not."
    )
    with conn:
        conn.execute(
            "INSERT INTO digests (kind, period_start, period_end, markdown, cited_message_ids, created_at)"
            " VALUES ('weekly', ?, ?, ?, ?, ?)",
            (to_iso(now - timedelta(days=7)), to_iso(now), digest, json.dumps(cited_ids(digest)),
             to_iso(now - timedelta(hours=3))),
        )
        conn.execute(
            "INSERT INTO investigations (question, context, markdown, cited_message_ids, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            ("Why did sentiment drop after v2.0?", json.dumps({"launch": "v2.0", "days": 14}), investigation,
             json.dumps(cited_ids(investigation)), to_iso(now - timedelta(hours=5))),
        )
        runs = []
        for offset in range(14, -1, -1):
            day = now - timedelta(days=offset, hours=2)
            runs.append(("triage", round(rng.uniform(0.03, 0.09), 4), "ok", None, day))
            runs.append(("theme", round(rng.uniform(0.01, 0.03), 4), "ok", None, day + timedelta(minutes=5)))
        runs.append(("digest", 0.41, "ok", None, now - timedelta(days=7, hours=3)))
        runs.append(("digest", 0.39, "ok", None, now - timedelta(hours=3)))
        runs.append(("investigate", 0.21, "ok", None, now - timedelta(hours=5)))
        runs.append(("triage", 0.0, "failed", "provider 529: overloaded", now - timedelta(days=3, hours=4)))
        runs.append(("theme", 0.0, "skipped_budget", "daily budget cap reached", now - timedelta(days=6, hours=1)))
        conn.executemany(
            "INSERT INTO agent_runs (agent, model, input_tokens, output_tokens, cost_usd, status, error,"
            " started_at, finished_at) VALUES (?, 'anthropic:demo', ?, ?, ?, ?, ?, ?, ?)",
            [(agent, int(cost * 200_000), int(cost * 20_000), cost, status, error, to_iso(at), to_iso(at))
             for agent, cost, status, error, at in runs],
        )
```

- [ ] **Step 4: Wire the CLI**

In `pulse/run.py`, add these imports:

```python
from pathlib import Path

import uvicorn

from pulse.demo import SERVER_NAME, demo_config, seed_demo
from pulse.web.app import create_app
from pulse.web.settings import WebSettings
```

Add the subcommands in `_parser()` (before `return parser`):

```python
    web = sub.add_parser("web", help="serve the dashboard on localhost")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8321)
    web.add_argument("--db", help="database file (default: pulse.toml's, or demo.db with --demo)")
    web.add_argument("--name", help="server name shown in the sidebar")
    web.add_argument("--demo", action="store_true", help="serve the demo database; no pulse.toml or API keys needed")
    seed = sub.add_parser("seed-demo", help="write a synthetic community to a demo database")
    seed.add_argument("--db", default="demo.db")
```

Add a helper above `main`:

```python
def _serve(settings: WebSettings, host: str, port: int) -> int:
    print(f"Discord Pulse dashboard on http://{host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0
```

In `main`, right after the `triage --force` check and before `load_config`, add:

```python
    if args.command == "seed-demo":
        try:
            counts = seed_demo(args.db, datetime.now(timezone.utc))
        except FileExistsError as e:
            print(f"seed-demo: {e}", file=sys.stderr)
            return 2
        print(f"demo database written to {args.db}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        print(f"serve it with: python -m pulse.run web --demo --db {args.db}")
        return 0
    if args.command == "web" and args.demo:
        db = Path(args.db or "demo.db")
        if not db.exists():
            print(f"web: {db} not found; run `python -m pulse.run seed-demo` first", file=sys.stderr)
            return 2
        settings = WebSettings(
            db_path=db, config=demo_config(db, datetime.now(timezone.utc)),
            server_name=args.name or SERVER_NAME, demo=True,
        )
        return _serve(settings, args.host, args.port)
```

And add this branch to the command dispatch (next to the other `elif args.command == ...` branches):

```python
    elif args.command == "web":
        conn.close()
        settings = WebSettings(
            db_path=Path(args.db) if args.db else config.db_path, config=config,
            server_name=args.name or "Discord server", llm_factory=build_llm,
        )
        return _serve(settings, args.host, args.port)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_demo.py tests/test_cli.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 6: Try it once by hand**

Run: `.venv/bin/python -m pulse.run seed-demo --db /tmp/pulse-demo.db && (.venv/bin/python -m pulse.run web --demo --db /tmp/pulse-demo.db --port 8399 & sleep 3; curl -s http://127.0.0.1:8399/ | grep -c "Community pulse"; kill %1)`
Expected: the seed summary line, then `1`.

- [ ] **Step 7: Commit**

```bash
git add pulse/demo.py pulse/run.py tests/test_demo.py tests/test_cli.py
git commit -m "feat: seed-demo dataset and the web command (with a no-keys demo mode)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 13: Plan 3 follow-ups and docs

**Files:**
- Modify: `pulse/agents/investigate.py`, `pulse/agents/digest.py`, `README.md`
- Test: `tests/test_investigate.py` (append), `tests/test_digest.py` (append)

**Interfaces:**
- Produces:
  - Investigate's `get_thread` returns up to `THREAD_LIMIT` messages around the requested one (so a message deep in a busy thread is always included), oldest first.
  - The weekly digest is not skipped as empty while open mod queue items exist: activity counts community messages plus open queue items in the input.
  - README section "Dashboard".

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_investigate.py`:

```python
def test_get_thread_includes_message_deep_in_a_busy_thread():
    conn = connect(":memory:")
    rows = [msg(f"m{i:02d}", f"reply {i}", minutes=i, channel_id="300", thread_id="300") for i in range(60)]
    upsert_messages(conn, rows, frozenset())
    box = Toolbox(conn, T0 + timedelta(days=1))
    result = json.loads(box.execute(ToolCall("1", "get_thread", {"message_id": "m50"})))
    ids = [r["message_id"] for r in result]
    assert "m50" in ids and len(ids) <= 30
    assert ids == sorted(ids)  # oldest first
```

Append to `tests/test_digest.py`:

```python
def test_weekly_digest_not_skipped_when_only_old_queue_items_are_open():
    from pulse.models import to_iso

    conn = connect(":memory:")
    old = NOW - timedelta(days=10)
    upsert_messages(conn, [msg("old", "still broken", minutes=(old - T0).total_seconds() / 60)], frozenset())
    set_triage(conn, "old", sentiment=-2, needs_reply=True, kind="bug")
    with conn:
        conn.execute(
            "INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('old', 'old', 'frustrated', 'open', ?)",
            (to_iso(old),),
        )
    backend = FakeBackend(handler=lambda user: BackendResult({"markdown": "## Needs attention\n[[msg:old]]"}, 10, 5))
    result = run_digest(conn, make_llm(conn, make_config(), backend), NOW)
    assert len(backend.calls) == 1
    assert result.cited_message_ids == ["old"]
```

(`test_digest.py` already imports everything this test uses.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_investigate.py tests/test_digest.py -q`
Expected: FAIL (`m50` missing from the thread; the digest makes no model call).

- [ ] **Step 3: Implement**

In `pulse/agents/investigate.py`, replace the `rows = self.conn.execute(...)` statement in `get_thread` with:

```python
        rows = self.conn.execute(
            "SELECT m.id, m.author_name, m.is_team, m.is_bot, m.created_at, m.content FROM messages m"
            " WHERE m.id = ? OR m.thread_id = ? OR m.reply_to_id = ?"
            " ORDER BY m.created_at, m.id LIMIT 1000",
            (mid, thread, mid),
        ).fetchall()
        at = next((i for i, r in enumerate(rows) if r["id"] == mid), 0)
        start = max(0, min(at - THREAD_LIMIT // 2, len(rows) - THREAD_LIMIT))
        rows = rows[start:start + THREAD_LIMIT]
```

In `pulse/agents/digest.py`, in `run_digest`, change the weekly activity line to count open queue items too:

```python
        activity = data["current"]["messages"] + sum(1 for m in data["messages"] if "queue_reason" in m)
```

Add this section to `README.md` after the commands section:

````markdown
## Dashboard

```bash
python -m pulse.run web              # http://127.0.0.1:8321, reads pulse.toml, agents on
python -m pulse.run seed-demo        # writes demo.db: a synthetic community, no keys needed
python -m pulse.run web --demo       # serves demo.db with agents off (good for pitching)
```

Views: Overview, Pain points, Bugs, Mod queue, Messages, Launch, Reports, Runs. Every view takes a window (7, 14, 30 or 90 days) and a channel; a channel includes its threads. Every message shows its author and an "Open in Discord" link, and "All from <author>" lists everything that person said.

- **Reply times**: median minutes to the first staff reply and how many messages have waited more than 24 hours, overall and per channel.
- **Pain point status**: mark a pain point Acknowledged, Fix in progress or Fix shipped with a note. Once shipped, the view compares volume and sentiment before and after.
- **Investigate and digests from the dashboard** run in the background and the page updates when they finish. They spend from the same daily cap as the pipeline; the Runs view shows every call and its cost.
- Messages imported before this version count under their own thread id when you filter by channel; re-run `ingest` once to attach threads to their parent channel.
- The dashboard has no login. It binds to 127.0.0.1 by default; don't expose it to the internet.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_investigate.py tests/test_digest.py -q`
Expected: PASS. Then `.venv/bin/pytest -q` once.

- [ ] **Step 5: Commit**

```bash
git add pulse/agents/investigate.py pulse/agents/digest.py README.md tests/test_investigate.py tests/test_digest.py
git commit -m "fix: thread window around the asked message; weekly digest with only old queue items; dashboard docs" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
