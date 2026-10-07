# Discord Pulse Plan 6: Community Analytics and the Shareable Report — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import reaction counts, add a Community view (when questions arrive vs when staff answer, newcomers, community helpers, most wanted), make the demo show it, and add a single-file HTML report for people who do not run the dashboard.

**Architecture:** One new module, `pulse/community.py`, holds all the new queries as plain functions over SQLite (no model calls). The dashboard gets one view (`/community`) and one SVG chart (`charts.heatmap`). `pulse/report.py` renders a standalone Jinja template with the dashboard CSS inlined, reusing the existing stats and the new community functions.

**Tech Stack:** Python 3.12 (`zoneinfo`), sqlite3, FastAPI + Jinja (existing), headless Chrome only for the README screenshot.

**Spec:** `docs/superpowers/specs/2026-09-29-discord-pulse-design.md`, section 16.

## Global Constraints

- No model calls and no new cost: everything in this plan is SQL and Python over the existing database.
- No new bot permission or intent. Reaction counts come with message history; live reaction changes are not tracked.
- Staff are `messages.is_team = 1`; community means `is_team = 0 AND is_bot = 0`. Bots are never counted as questions, newcomers or helpers.
- Every new query honours the dashboard's channel filter through `pulse.stats.scope_clause(channels)` (alias `m` for the message table).
- Times on the Community view and in the report's heatmaps are shown in `[server] timezone` (default `"UTC"`), validated with `zoneinfo`.
- The shareable report is one HTML file with inline CSS, no JavaScript and no external requests. Without `--with-names` it shows staff as "staff", everyone else as "a member", helpers as "Helper 1…", and no avatars; Discord links and excerpts stay. Default output `reports/pulse-<YYYY-MM-DD>.html`; `reports/` is git-ignored.
- Screenshots and demo data use only the synthetic Acme demo, never a real server's data.
- Tests never touch the network. Every commit message ends with exactly `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A message re-imported with fewer or no reactions: its old counts are replaced, never summed or left behind (Task 1 `test_reimport_replaces_counts`).
2. Newcomer edge cases: an author's first message is before the window, the first "reply" is the author's own, the reply arrives after 48 hours, the reply is in the thread the message started (Task 2 `test_newcomers_reply_and_return`).
3. Helper edge cases: answering your own question, staff and bot answers, answers to messages that did not need a reply, answers inside a thread to its first message (Task 2 `test_helpers_rank_members_who_answer_others`).
4. An empty database or a window with no activity: the Community page and the report render with empty states, not errors (Task 3 `test_community_empty_db`, Task 5 `test_report_on_empty_db`).
5. The report leaks no author names, avatars or `<script>` without `--with-names`, including through digest citations (Task 5 `test_report_anonymizes_by_default`).

## Files

| File | Responsibility | Task |
|---|---|---|
| `pulse/models.py` | `Message.reactions`, `merge_reactions` | 1 |
| `pulse/db.py` | `reactions` table | 1 |
| `pulse/sources/file_source.py`, `pulse/sources/bot_source.py` | read reaction counts | 1 |
| `pulse/store.py` | replace a message's reactions on upsert | 1 |
| `pulse/config.py` | `[server] timezone` | 1 |
| `pulse/community.py` | heatmap, coverage gaps, newcomers, helpers, most wanted, top reacted | 2 |
| `pulse/web/charts.py`, `pulse/web/views/community.py`, `community.html`, nav | Community view | 3 |
| `pulse/demo.py` | newcomers, helper replies and reactions in the demo | 4 |
| `pulse/report.py`, `pulse/web/templates/report.html`, `pulse/run.py`, `pulse/citations.py` | shareable report | 5 |

---

### Task 1: Reaction counts and the server timezone

**Files:**
- Modify: `pulse/models.py`, `pulse/db.py`, `pulse/sources/file_source.py`, `pulse/sources/bot_source.py`, `pulse/store.py`, `pulse/config.py`, `pulse.toml.example`
- Test: `tests/test_reactions.py`

**Interfaces:**
- Produces: `Message.reactions: tuple[tuple[str, int], ...] = ()` (last field); `merge_reactions(pairs) -> tuple[tuple[str, int], ...]` (sums per emoji name, drops blank names and non-positive or non-numeric counts, sorted by count desc then name); table `reactions(message_id, emoji, count, PRIMARY KEY (message_id, emoji))`; `upsert_messages` replaces a message's rows in `reactions` on every insert or update; `Config.timezone: str = "UTC"` (last field) from `[server] timezone`.

Note: a CSV import carries no reactions, so re-importing a message from CSV clears its counts. If an existing test compares whole `Message` objects read from `tests/fixtures/dce_*.json` and that fixture has a `reactions` array, update the expected value to include the merged reactions and report it.

- [ ] **Step 1: Write the failing tests**

`tests/test_reactions.py`:

```python
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from pulse.config import ConfigError, load_config
from pulse.db import connect
from pulse.models import merge_reactions
from pulse.sources.bot_source import message_from_discord
from pulse.sources.file_source import FileSource
from pulse.store import upsert_messages
from tests.fakes import T0, msg


def export(tmp_path, message_extra):
    folder = tmp_path / "imports"
    folder.mkdir(exist_ok=True)
    message = {"id": "1", "type": "Default", "timestamp": "2026-09-28T12:00:00+00:00",
               "content": "add a Django guide", "author": {"id": "u1", "name": "alice", "isBot": False}}
    message.update(message_extra)
    (folder / "help.json").write_text(json.dumps({
        "guild": {"id": "900", "name": "Acme"},
        "channel": {"id": "100", "type": "GuildTextChat", "name": "help"},
        "messages": [message],
    }), encoding="utf-8")
    return folder


def rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT message_id, emoji, count FROM reactions ORDER BY count DESC, emoji")]


def test_merge_reactions_sums_and_drops_junk():
    assert merge_reactions([("👍", 3), ("pepe", 1), ("👍", 2), ("", 5), ("x", 0), (None, 1), ("y", "bad")]) == (
        ("👍", 5), ("pepe", 1))


def test_file_import_stores_reaction_counts(tmp_path):
    source = FileSource(export(tmp_path, {"reactions": [
        {"emoji": {"id": "", "name": "👍", "code": "thumbsup"}, "count": 3},
        {"emoji": {"id": "123", "name": "pepe", "code": "pepe"}, "count": 1},
        {"emoji": {"name": "👍"}, "count": 2},
    ]}))
    [m] = list(source.fetch())
    assert source.errors == [] and m.reactions == (("👍", 5), ("pepe", 1))
    conn = connect(":memory:")
    upsert_messages(conn, [m], frozenset())
    assert rows(conn) == [("1", "👍", 5), ("1", "pepe", 1)]


def test_export_without_reactions_has_none(tmp_path):
    [m] = list(FileSource(export(tmp_path, {})).fetch())
    assert m.reactions == ()


def test_reimport_replaces_counts():
    conn = connect(":memory:")
    m = replace(msg("1"), reactions=(("👍", 2),))
    upsert_messages(conn, [m], frozenset())
    upsert_messages(conn, [replace(m, reactions=(("👍", 7), ("🎉", 1)))], frozenset())
    assert rows(conn) == [("1", "👍", 7), ("1", "🎉", 1)]
    upsert_messages(conn, [replace(m, reactions=())], frozenset())
    assert rows(conn) == []


def test_bot_mapping_reads_reactions():
    raw = NS(
        id=5, type=NS(name="default"), channel=NS(id=100, name="help"), guild=NS(id=900), content="hi",
        created_at=T0, edited_at=None, reference=None,
        author=NS(id=1, name="a", display_name="A", bot=False, display_avatar=None),
        reactions=[NS(emoji=NS(name="👍"), count=4), NS(emoji="🎉", count=1), NS(emoji=NS(name="x"), count=0)],
    )
    assert message_from_discord(raw, "900").reactions == (("👍", 4), ("🎉", 1))


BASE = (
    '[server]\nguild_id = "1"\n{tz}\n[models]\ntriage = "anthropic:m"\ntheme = "anthropic:m"\n'
    'digest = "anthropic:m"\ninvestigate = "anthropic:m"\n[pricing."anthropic:m"]\ninput = 1\noutput = 2\n'
)


def test_server_timezone(tmp_path):
    path = tmp_path / "pulse.toml"
    path.write_text(BASE.format(tz=""))
    assert load_config(path, {}, require_keys=False).timezone == "UTC"
    path.write_text(BASE.format(tz='timezone = "America/New_York"'))
    assert load_config(path, {}, require_keys=False).timezone == "America/New_York"
    path.write_text(BASE.format(tz='timezone = "Mars/Olympus"'))
    with pytest.raises(ConfigError, match="timezone"):
        load_config(path, {}, require_keys=False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_reactions.py -q`
Expected: FAIL with `ImportError: cannot import name 'merge_reactions'`

- [ ] **Step 3: Write the implementation**

`pulse/models.py`: add `reactions: tuple[tuple[str, int], ...] = ()` as the last `Message` field, and:

```python
def merge_reactions(pairs) -> tuple[tuple[str, int], ...]:
    """Sum counts per emoji name; drop blank names and counts that are not positive whole
    numbers; most used first, then by name."""
    totals: dict[str, int] = {}
    for name, count in pairs:
        name = str(name or "").strip()
        try:
            n = int(count)
        except (TypeError, ValueError):
            continue
        if name and n > 0:
            totals[name] = totals.get(name, 0) + n
    return tuple(sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])))
```

`pulse/db.py`: add to `SCHEMA` after the `messages` table:

```sql
CREATE TABLE IF NOT EXISTS reactions (
    message_id TEXT NOT NULL REFERENCES messages(id),
    emoji TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (message_id, emoji)
);
```

`pulse/sources/file_source.py`: import `merge_reactions` from `pulse.models` and pass this keyword in the JSON `Message(...)` call:

```python
                        reactions=merge_reactions(
                            ((r.get("emoji") or {}).get("name") or (r.get("emoji") or {}).get("code"), r.get("count"))
                            for r in raw.get("reactions") or [] if isinstance(r, dict)
                        ),
```

`pulse/sources/bot_source.py`: import `merge_reactions` and pass in `message_from_discord`'s `Message(...)`:

```python
        reactions=merge_reactions(
            (getattr(r.emoji, "name", None) or str(r.emoji), getattr(r, "count", 0))
            for r in getattr(msg, "reactions", None) or []
        ),
```

`pulse/store.py`:

```python
def _store_reactions(conn: sqlite3.Connection, m: Message) -> None:
    conn.execute("DELETE FROM reactions WHERE message_id = ?", (m.id,))
    if m.reactions:
        conn.executemany(
            "INSERT INTO reactions (message_id, emoji, count) VALUES (?, ?, ?)",
            [(m.id, emoji, count) for emoji, count in m.reactions],
        )
```

and in `upsert_messages` call `_store_reactions(conn, m)` right after the `_INSERT` execute (before `continue`) and right after the `_UPDATE` execute.

`pulse/config.py`: `from zoneinfo import ZoneInfo, ZoneInfoNotFoundError`; add `timezone: str = "UTC"` as the last `Config` field; in `load_config` after `guild_id` is read:

```python
    tz = str(server.get("timezone", "UTC"))
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f'[server] timezone {tz!r} is not a known time zone (for example "America/New_York")') from e
```

and pass `timezone=tz`. In `pulse.toml.example`, under `[server]`, add:

```toml
timezone = "UTC"                    # for the Community view's weekday x hour charts, e.g. "America/New_York"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_reactions.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/models.py pulse/db.py pulse/sources/file_source.py pulse/sources/bot_source.py pulse/store.py pulse/config.py pulse.toml.example tests/test_reactions.py
git commit -m "feat: import reaction counts; [server] timezone

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Community analytics module

**Files:**
- Create: `pulse/community.py`
- Test: `tests/test_community.py`

**Interfaces:**
- Consumes: `pulse.stats.scope_clause`, `pulse.models.from_iso/to_iso`, the `reactions` table (Task 1).
- Produces:
  - `WEEKDAYS = ("Mon", ..., "Sun")`
  - `activity_heatmap(conn, start, end, tz="UTC", channels=None) -> {"questions": 7x24 list, "staff": 7x24 list, "timezone": tz}` — weekday 0 = Monday, local time in `tz`; questions = community messages triaged needs-reply; staff = `is_team` messages; bots ignored.
  - `coverage_gaps(heat, top=3) -> list[{"label", "questions", "staff"}]` — hours with at least one question, fewest staff first, then most questions; label like `"Tue 02:00–03:00"`.
  - `newcomers(conn, start, end, channels=None) -> {"count", "replied", "returned", "weekly": [{"week": "YYYY-MM-DD" (a Monday), "count"}], "unreplied_ids": [up to 10, newest first]}`
  - `helpers(conn, start, end, channels=None, limit=10) -> list[{"author_id", "author_name", "answers", "helped", "last_at", "sample_id"}]`
  - `most_wanted(conn, start, end, channels=None, limit=8)` and `top_reacted(conn, start, end, channels=None, limit=5)` -> `list[{"message_id", "total", "emojis": [(emoji, count), ...up to 4]}]`

Rules (spec 16.2):
- **Newcomer:** a community author whose earliest message within the channel scope is inside `[start, end)`. **Replied:** another non-bot author posted, within 48 hours after it, a direct reply to it, a message in the thread it started (`thread_id` = its id), or a later message in the same thread. **Returned:** the author posted anything at least 24 hours after it.
- **Helper answer:** a community message in the window that directly replies to, or (in a thread) follows, a needs-reply message by someone else that is earlier. In a thread the question is the message the thread was started from (`thread_id`) or the thread's first message. One answering message counts once.

- [ ] **Step 1: Write the failing tests**

`tests/test_community.py`:

```python
from dataclasses import replace
from datetime import date, timedelta
from zoneinfo import ZoneInfo

from pulse.community import activity_heatmap, coverage_gaps, helpers, most_wanted, newcomers, top_reacted
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import T0, msg, set_triage

START, END = T0 - timedelta(days=1), T0 + timedelta(days=3)
TEAM = frozenset({"t1"})


def m(id, *, minutes=0, author="u1", channel="100", thread=None, reply_to=None, bot=False, reactions=()):
    base = msg(id, f"text {id}", minutes=minutes, channel_id=channel, thread_id=thread,
               author_id=author, author_name=author, reply_to_id=reply_to, is_bot=bot)
    return replace(base, reactions=reactions)


def db(messages, labels):
    conn = connect(":memory:")
    upsert_messages(conn, messages, TEAM)
    for mid, kw in labels.items():
        set_triage(conn, mid, **kw)
    return conn


def total(grid):
    return sum(map(sum, grid))


def test_heatmap_counts_questions_and_staff_in_the_configured_zone():
    conn = db(
        [m("q"), m("s", minutes=60, author="t1"), m("c", minutes=5, author="u2"),
         m("b", minutes=6, author="bot", bot=True), m("other", minutes=7, author="u3", channel="200")],
        {"q": dict(needs_reply=True), "c": dict(needs_reply=False), "b": dict(needs_reply=True),
         "other": dict(needs_reply=True)},
    )
    utc = activity_heatmap(conn, START, END)
    assert utc["questions"][T0.weekday()][T0.hour] == 2
    assert utc["staff"][T0.weekday()][T0.hour + 1] == 1
    assert total(utc["questions"]) == 2 and total(utc["staff"]) == 1
    ny = activity_heatmap(conn, START, END, "America/New_York")
    local = T0.astimezone(ZoneInfo("America/New_York"))
    assert ny["questions"][local.weekday()][local.hour] == 2 and ny["timezone"] == "America/New_York"
    assert total(activity_heatmap(conn, START, END, channels=("100",))["questions"]) == 1


def test_coverage_gaps_prefers_unstaffed_busy_hours():
    q = [[0] * 24 for _ in range(7)]
    s = [[0] * 24 for _ in range(7)]
    q[0][9], s[0][9] = 5, 4
    q[1][2] = 3
    q[2][14] = 1
    q[3][3] = 6
    q[6][23] = 1
    s[6][23] = 9
    gaps = coverage_gaps({"questions": q, "staff": s})
    assert [g["label"] for g in gaps] == ["Thu 03:00–04:00", "Tue 02:00–03:00", "Wed 14:00–15:00"]
    assert gaps[0] == {"label": "Thu 03:00–04:00", "questions": 6, "staff": 0}
    assert coverage_gaps({"questions": [[0] * 24 for _ in range(7)], "staff": s}) == []
    assert coverage_gaps({"questions": q, "staff": s}, top=5)[-1]["label"] == "Sun 23:00–00:00"


def test_newcomers_reply_and_return():
    msgs = [
        m("old", minutes=-10 * 24 * 60, author="u3"), m("u3b", minutes=30, author="u3"),
        m("n1", author="u1"), m("r1", minutes=30, author="t1", reply_to="n1"),
        m("n2", minutes=120, author="u2"), m("n2b", minutes=120 + 2 * 24 * 60, author="u2"),
        m("n4", minutes=200, author="u4", thread="300"), m("r4", minutes=230, author="u5", thread="300"),
        m("n6", minutes=300, author="u6"), m("r6", minutes=310, author="u7", thread="n6"),
        m("n8", minutes=400, author="u8"), m("own", minutes=410, author="u8", reply_to="n8"),
        m("late", minutes=500, author="u9"), m("lr", minutes=500 + 3 * 24 * 60, author="t1", reply_to="late"),
        m("botnew", minutes=600, author="b", bot=True),
    ]
    nc = newcomers(db(msgs, {}), START, END)
    assert nc["count"] == 8
    assert nc["replied"] == 3
    assert nc["returned"] == 1
    assert nc["unreplied_ids"] == ["late", "n8", "r6", "r4", "n2"]
    assert sum(w["count"] for w in nc["weekly"]) == 8
    assert all(date.fromisoformat(w["week"]).weekday() == 0 for w in nc["weekly"])
    assert newcomers(db(msgs, {}), START, END, channels=("200",))["count"] == 0


def test_helpers_rank_members_who_answer_others():
    msgs = [
        m("q1", author="u1"), m("a1", minutes=10, author="u2", reply_to="q1"),
        m("q2", minutes=20, author="u3"), m("a2", minutes=30, author="u2", reply_to="q2"),
        m("q3", minutes=40, author="u4", thread="300"), m("a3", minutes=50, author="u5", thread="300"),
        m("self", minutes=60, author="u1", reply_to="q1"),
        m("staff", minutes=70, author="t1", reply_to="q2"),
        m("chat", minutes=80, author="u6"), m("a4", minutes=90, author="u7", reply_to="chat"),
        m("botq", minutes=95, author="u8"), m("bota", minutes=96, author="bot", bot=True, reply_to="botq"),
    ]
    labels = {"q1": dict(needs_reply=True), "q2": dict(needs_reply=True), "q3": dict(needs_reply=True),
              "chat": dict(needs_reply=False), "botq": dict(needs_reply=True)}
    board = helpers(db(msgs, labels), START, END)
    assert [(h["author_id"], h["answers"], h["helped"]) for h in board] == [("u2", 2, 2), ("u5", 1, 1)]
    assert board[0]["sample_id"] == "a2" and board[0]["author_name"] == "u2"
    assert helpers(db(msgs, labels), START, END, channels=("999",)) == []


def test_most_wanted_and_top_reacted():
    msgs = [
        m("f1", author="u1", reactions=(("👍", 9), ("❤️", 2))),
        m("f2", minutes=5, author="u2", reactions=(("👍", 3),)),
        m("f3", minutes=6, author="u3"),
        m("p1", minutes=7, author="u4", reactions=(("🎉", 20),)),
        m("bot", minutes=8, author="b", bot=True, reactions=(("👍", 50),)),
    ]
    labels = {"f1": dict(kind="feature_request"), "f2": dict(kind="feature_request"),
              "f3": dict(kind="feature_request"), "p1": dict(kind="praise")}
    conn = db(msgs, labels)
    wanted = most_wanted(conn, START, END)
    assert [(w["message_id"], w["total"]) for w in wanted] == [("f1", 11), ("f2", 3)]
    assert wanted[0]["emojis"] == [("👍", 9), ("❤️", 2)]
    assert [t["message_id"] for t in top_reacted(conn, START, END)] == ["p1", "f1", "f2"]
    assert most_wanted(conn, START, END, channels=("200",)) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_community.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.community'`

- [ ] **Step 3: Write the implementation**

`pulse/community.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_community.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/community.py tests/test_community.py
git commit -m "feat: community analytics: coverage heatmap, newcomers, helpers, most wanted

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The Community view

**Files:**
- Modify: `pulse/web/charts.py` (`heatmap`), `pulse/web/context.py` (nav + `PATHS`), `pulse/web/app.py` (router), `pulse/web/static/pulse.css`
- Create: `pulse/web/views/community.py`, `pulse/web/templates/community.html`
- Test: `tests/test_web_community.py`

**Interfaces:**
- Consumes: Task 2 functions; `Config.timezone` (Task 1); `cards_by_ids`, `render`, `get_conn`, `get_filters`, `message_card`, `range_selector`.
- Produces: `charts.heatmap(grid, label) -> Markup` (7x24 SVG, cells shaded relative to the grid's peak, each cell titled `"Mon 09:00 · 3"`); `GET /community`; nav item "Community" after "Mod queue"; `PATHS["community"] = "/community"`.

- [ ] **Step 1: Write the failing tests**

`tests/test_web_community.py`:

```python
from dataclasses import replace

from pulse.db import connect
from pulse.web.charts import heatmap
from tests.web_fakes import CONFIG, make_client


def test_heatmap_svg():
    grid = [[0] * 24 for _ in range(7)]
    grid[1][9] = 4
    grid[1][10] = 2
    svg = str(heatmap(grid, "Questions by hour"))
    assert svg.startswith('<svg class="heat"') and 'aria-label="Questions by hour"' in svg
    assert "<title>Tue 09:00 · 4</title>" in svg and 'fill-opacity="1.00"' in svg
    assert svg.count("<rect") == 7 * 24
    assert "fill-opacity" not in str(heatmap([[0] * 24 for _ in range(7)], "empty"))


def test_community_page_renders_sections(tmp_path):
    client = make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    with conn:
        conn.execute("INSERT INTO reactions (message_id, emoji, count) VALUES ('p1', '🎉', 6)")
    conn.close()
    html = client.get("/community?days=7").text
    for heading in ("When questions arrive", "Coverage gaps", "Newcomers", "Community helpers", "Most reacted"):
        assert heading in html
    assert '<svg class="heat"' in html and "🎉 6" in html
    assert 'href="/community' in client.get("/").text


def test_community_respects_channel_filter(tmp_path):
    html = make_client(tmp_path).get("/community?days=7&channel=999").text
    assert "No feature requests with reactions" in html


def test_community_uses_configured_timezone(tmp_path):
    html = make_client(tmp_path, config=replace(CONFIG, timezone="Asia/Tokyo")).get("/community").text
    assert "Asia/Tokyo" in html


def test_community_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/community")
    assert r.status_code == 200 and "No newcomers" in r.text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_community.py -q`
Expected: FAIL with `ImportError: cannot import name 'heatmap'`

- [ ] **Step 3: Write the implementation**

`pulse/web/charts.py` (import `WEEKDAYS` from `pulse.community`):

```python
def heatmap(grid: list[list[int]], label: str) -> Markup:
    """7 x 24 grid (Mon..Sun x hour) as SVG cells shaded relative to the busiest cell."""
    cw, ch, left, top = 22, 18, 34, 18
    w, h = left + 24 * cw, top + 7 * ch
    peak = max((max(row) for row in grid), default=0)
    parts = [f'<svg class="heat" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(label)}">']
    for hour in range(0, 24, 3):
        parts.append(f'<text class="ax" x="{left + hour * cw + cw / 2:.0f}" y="12" text-anchor="middle">{hour:02d}</text>')
    for d, row in enumerate(grid):
        y = top + d * ch
        parts.append(f'<text class="ax" x="{left - 6}" y="{y + ch - 5}" text-anchor="end">{WEEKDAYS[d]}</text>')
        for hour, v in enumerate(row):
            shade = f' fill-opacity="{0.15 + 0.85 * v / peak:.2f}"' if v and peak else ""
            parts.append(
                f'<rect class="{"hm" if v else "hm0"}" x="{left + hour * cw + 1}" y="{y + 1}" width="{cw - 2}"'
                f' height="{ch - 2}" rx="2"{shade}><title>{WEEKDAYS[d]} {hour:02d}:00 · {v}</title></rect>'
            )
    parts.append("</svg>")
    return Markup("".join(parts))
```

Append to `pulse/web/static/pulse.css`:

```css
.heat{width:100%;height:auto;display:block}
.heat .hm{fill:var(--accent)}
.heat .hm0{fill:var(--line)}
.heat .ax{fill:var(--muted);font:10px var(--mono)}
.react{font-variant-numeric:tabular-nums}
```

`pulse/web/context.py`: add `"community": "/community"` to `PATHS` and `("community", "Community", None, False)` to `nav` right after the `queue` entry.

`pulse/web/views/community.py`:

```python
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from pulse import community
from pulse.web.cards import cards_by_ids
from pulse.web.charts import heatmap
from pulse.web.deps import get_conn, get_filters, render
from pulse.web.filters import Filters

router = APIRouter()


def _with_cards(conn, items: list[dict]) -> list[dict]:
    cards = {c["message_id"]: c for c in cards_by_ids(conn, [i["message_id"] for i in items])}
    return [{**i, "card": cards[i["message_id"]]} for i in items if i["message_id"] in cards]


@router.get("/community", response_class=HTMLResponse)
def community_view(request: Request, conn=Depends(get_conn), f: Filters = Depends(get_filters)):
    tz = request.app.state.settings.config.timezone
    heat = community.activity_heatmap(conn, f.start, f.end, tz, f.channels)
    nc = community.newcomers(conn, f.start, f.end, f.channels)
    return render(
        request, "community.html", conn, f, "community",
        tz=tz,
        heat_questions=heatmap(heat["questions"], "Questions needing a reply by weekday and hour"),
        heat_staff=heatmap(heat["staff"], "Staff messages by weekday and hour"),
        gaps=community.coverage_gaps(heat),
        nc=nc,
        unreplied=cards_by_ids(conn, nc["unreplied_ids"]),
        helpers=community.helpers(conn, f.start, f.end, f.channels),
        wanted=_with_cards(conn, community.most_wanted(conn, f.start, f.end, f.channels)),
        reacted=_with_cards(conn, community.top_reacted(conn, f.start, f.end, f.channels)),
    )
```

Register it in `pulse/web/app.py` (add `community` to the views import and to the router tuple).

`pulse/web/templates/community.html`:

```html
{% extends "base.html" %}
{% from "_macros.html" import message_card, range_selector %}
{% block title %}Community{% endblock %}
{% block main %}
<div class="head">
  <div><h1>Community</h1><p class="sub">When questions arrive and when staff answer, who is new, who helps others, and what people want most. Times in {{ tz }}.</p></div>
  {{ range_selector(f, "/community") }}
</div>

<section class="panel">
  <div class="panel-head"><h2>When questions arrive</h2><span class="note">messages needing a reply vs staff messages, by weekday and hour ({{ tz }})</span></div>
  <div class="grid2">
    <div><p class="label">Questions needing a reply</p>{{ heat_questions }}</div>
    <div><p class="label">Staff messages</p>{{ heat_staff }}</div>
  </div>
  <h3>Coverage gaps</h3>
  {% if gaps %}
  <ul class="gaps">{% for g in gaps %}<li><b>{{ g.label }}</b>: {{ g.questions }} question{{ 's' if g.questions != 1 }}, {{ g.staff }} staff message{{ 's' if g.staff != 1 }}</li>{% endfor %}</ul>
  {% else %}<p class="empty">No questions in this window.</p>{% endif %}
</section>

<div class="grid2">
  <section class="panel">
    <div class="panel-head"><h2>Newcomers</h2><span class="note">first message in this window</span></div>
    {% if nc.count %}
    <div class="strip">
      <div class="stat"><span class="label">New members</span><span class="big">{{ nc.count }}</span></div>
      <div class="stat"><span class="label">Got a reply within 48h</span><span class="big">{{ nc.replied }}</span><span class="delta mut">{{ (nc.replied * 100 / nc.count)|round|int }}%</span></div>
      <div class="stat"><span class="label">Came back</span><span class="big">{{ nc.returned }}</span><span class="delta mut">posted again a day or more later</span></div>
    </div>
    <p class="pmeta">Per week: {% for w in nc.weekly %}{{ w.week }} <b>{{ w.count }}</b>{% if not loop.last %} · {% endif %}{% endfor %}</p>
    <h3>First messages with no reply</h3>
    <div class="msgs">{% for c in unreplied %}{{ message_card(c, now, f) }}{% else %}<p class="empty">Every newcomer got a reply.</p>{% endfor %}</div>
    {% else %}<p class="empty">No newcomers in this window.</p>{% endif %}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Community helpers</h2><span class="note">members answering other people's questions</span></div>
    {% if helpers %}
    <div class="tbl-wrap"><table class="chan">
      <thead><tr><th>Member</th><th class="num">Answers</th><th class="num">People helped</th><th>Last answer</th></tr></thead>
      <tbody>{% for h in helpers %}<tr>
        <td><a href="/messages{{ f.qs(author_id=h.author_id) }}">{{ h.author_name }}</a></td>
        <td class="num">{{ h.answers }}</td><td class="num">{{ h.helped }}</td><td>{{ h.last_at|age(now) }}</td>
      </tr>{% endfor %}</tbody>
    </table></div>
    {% else %}<p class="empty">No member answers in this window.</p>{% endif %}
  </section>
</div>

<div class="grid2">
  <section class="panel">
    <div class="panel-head"><h2>Most wanted</h2><span class="note">feature requests by reactions</span></div>
    <div class="msgs">{% for w in wanted %}{% call message_card(w.card, now, f) %}{% for e, n in w.emojis %}<span class="pill kind react">{{ e }} {{ n }}</span>{% endfor %}{% endcall %}{% else %}<p class="empty">No feature requests with reactions in this window.</p>{% endfor %}</div>
  </section>
  <section class="panel">
    <div class="panel-head"><h2>Most reacted</h2><span class="note">any message</span></div>
    <div class="msgs">{% for r in reacted %}{% call message_card(r.card, now, f) %}{% for e, n in r.emojis %}<span class="pill kind react">{{ e }} {{ n }}</span>{% endfor %}{% endcall %}{% else %}<p class="empty">No reactions in this window.</p>{% endfor %}</div>
  </section>
</div>
{% endblock %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_community.py -q` then `.venv/bin/pytest -q`
Expected: all pass (the nav gained an item; update any existing test that counts nav links exactly and report it)

- [ ] **Step 5: Commit**

```bash
git add pulse/web/charts.py pulse/web/context.py pulse/web/app.py pulse/web/static/pulse.css pulse/web/views/community.py pulse/web/templates/community.html tests/test_web_community.py
git commit -m "feat: Community view with coverage heatmaps, newcomers, helpers and most wanted

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Demo data for the Community view, and its screenshot

**Files:**
- Modify: `pulse/demo.py`, `README.md`, `tests/test_demo.py`
- Create: `docs/screenshots/community.png`

**Interfaces:**
- Consumes: `Message.reactions` (Task 1), `pulse.community` (Task 2), the Community view (Task 3).
- Produces: a demo with newcomers (some answered by staff, some by community helpers, some returning), helper replies to unanswered questions, and reactions on feature requests and praise.

All new randomness uses a second generator, `rng2 = random.Random(7)`, so the existing demo (`rng`, seed 42) produces exactly the same messages as before; the new messages are added on top.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_demo.py` (reuse that file's existing imports and seeding helper; if it has none, seed with `seed_demo(tmp_path / "demo.db", datetime.now(timezone.utc))`):

```python
def test_demo_has_community_data(tmp_path):
    from datetime import datetime, timedelta, timezone

    from pulse import community
    from pulse.db import connect
    from pulse.demo import seed_demo

    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    seed_demo(tmp_path / "demo.db", now)
    conn = connect(tmp_path / "demo.db")
    start = now - timedelta(days=30)
    nc = community.newcomers(conn, start, now)
    assert nc["count"] >= 8 and 0 < nc["replied"] < nc["count"] and nc["returned"] > 0
    board = community.helpers(conn, start, now)
    assert len(board) >= 2 and all(not h["author_id"].startswith("t") for h in board)
    assert community.most_wanted(conn, start, now) and community.top_reacted(conn, start, now)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/test_demo.py -q`
Expected: FAIL (`most_wanted` empty, too few newcomers)

- [ ] **Step 3: Write the implementation**

In `pulse/demo.py`, add constants near `USERS`:

```python
HELPERS = ("carol", "hal", "quinn")
HELPER_REPLIES = (
    "I hit this too: pinning acme to 2.0.1 fixed it until the patch ships",
    "the token exchange step is in the migration guide under Auth, step 3",
    "try `acme login --reset`, that cleared it for me",
)
NEWCOMERS = (
    ("ana", "hi all! where do I find the API key page?"),
    ("ben", "new here: does acme support Bun yet?"),
    ("cy", "first time trying acme deploy, it hangs at 'uploading'"),
    ("dee", "hello! is there a Python 3.13 wheel for M1?"),
    ("eli", "just joined, how do I rotate tokens?"),
    ("fay", "hi, is the free tier rate limit per key or per org?"),
    ("gil", "newbie question: what's the difference between acme dev and acme run?"),
    ("hana", "hey! any example for the Next.js app router?"),
    ("ike", "first post: the docs link for webhooks 404s"),
    ("jun", "hi, can I self-host the dashboard?"),
    ("kit", "new user: getting 401 after upgrading to v2"),
    ("lou", "hello, does acme work behind a corporate proxy?"),
)
```

In `seed_demo`:
- after `rng = random.Random(42)` add `rng2 = random.Random(7)`;
- give `add()` a `reactions=()` keyword passed to `Message(..., reactions=reactions)`;
- in the main loop, pass `reactions=_demo_reactions(spec["kind"], rng2)` to the `add(...)` that creates each community message, with

```python
def _demo_reactions(kind: str, rng2: random.Random) -> tuple[tuple[str, int], ...]:
    if kind == "feature_request":
        return merge_reactions([("👍", rng2.randint(1, 14)), ("❤️", rng2.randint(0, 3))])
    if kind == "praise":
        return merge_reactions([("🎉", rng2.randint(0, 6))])
    return ()
```

  (import `merge_reactions` from `pulse.models`);
- right after the existing staff-reply block (`if needs and rng.random() < 0.45: ...`), add an `elif` so unanswered questions sometimes get a community answer:

```python
            elif needs and rng2.random() < 0.4:
                helper = rng2.choice(HELPERS)
                reply_at = at + timedelta(minutes=rng2.randint(5, 240))
                if helper != author and reply_at < now:
                    add(channel, f"u-{helper}", helper, rng2.choice(HELPER_REPLIES), reply_at,
                        thread=thread, parent=parent, reply_to=None if thread else mid)
```

- after the main loop (before `conn = connect(path)`), add the newcomers:

```python
    for name, question in NEWCOMERS:
        at = today - timedelta(days=rng2.randint(0, 20), minutes=rng2.randint(0, 24 * 60 - 1))
        if at >= now:
            continue
        label = {"sentiment": rng2.choice((-1, 0)), "kind": "question", "needs_reply": True, "topics": []}
        mid = add(HELP, f"n-{name}", name, question, at, label=label)
        roll = rng2.random()
        if roll < 0.35:
            staff = rng2.choice(sorted(TEAM))
            responder = (staff, TEAM[staff], rng2.choice(STAFF_REPLIES))
        elif roll < 0.65:
            helper = rng2.choice(HELPERS)
            responder = (f"u-{helper}", helper, rng2.choice(HELPER_REPLIES))
        else:
            responder = None
        if responder and at + timedelta(hours=2) < now:
            add(HELP, *responder, at + timedelta(minutes=rng2.randint(10, 600)), reply_to=mid)
        later = at + timedelta(days=rng2.randint(1, 6))
        if rng2.random() < 0.5 and later < now:
            add(GENERAL, f"n-{name}", name, "thanks, got it working!", later,
                label={"sentiment": 1, "kind": "praise", "needs_reply": False, "topics": []})
```

Then refresh the screenshot (headless Chrome, demo data only), from the repo root:

```bash
SHOT=$(mktemp -d)
.venv/bin/python -m pulse.run seed-demo --db "$SHOT/demo.db"
.venv/bin/python -m pulse.run web --demo --db "$SHOT/demo.db" --port 8399 > "$SHOT/web.log" 2>&1 &
SERVER=$!; sleep 3
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new --disable-gpu --hide-scrollbars \
  --window-size=1440,1700 --screenshot=docs/screenshots/community.png "http://127.0.0.1:8399/community?days=30"
kill $SERVER; rm -rf "$SHOT"
```

Open `docs/screenshots/community.png` and check it shows the Acme demo ("Acme SDK Community (demo)" in the sidebar) with both heatmaps, newcomers, helpers and most wanted filled in. In `README.md`'s Screenshots section add a row:

```markdown
| Community | |
|---|---|
| ![Community: when questions arrive vs when staff answer, newcomers, helpers, most wanted](docs/screenshots/community.png) | |
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_demo.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/demo.py tests/test_demo.py README.md docs/screenshots/community.png
git commit -m "feat: demo newcomers, helper answers and reactions; Community screenshot

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The shareable single-file report

**Files:**
- Create: `pulse/report.py`, `pulse/web/templates/report.html`
- Modify: `pulse/citations.py` (`render_html(..., anonymize=False)`), `pulse/run.py` (`report` command), `.gitignore`, `README.md`
- Test: `tests/test_report.py`

**Interfaces:**
- Consumes: `pulse.stats.period_summary/sentiment_series/theme_scores`, `pulse.theme_status.statuses`, `pulse.web.queries.theme_daily/latest_digest/launch_markers`, `pulse.web.charts.sentiment_chart/sparkline/heatmap`, `pulse.web.context.open_queue_count`, `pulse.web.cards.cards_by_ids`, `pulse.community` (Task 2), `Config.timezone`.
- Produces: `render_html(markdown_text, conn, *, anonymize=False)` (citation text becomes "@staff" / "@a member"); `build_report(conn, config, now, *, days=7, with_names=False, server_name="Discord server") -> str`; CLI `report [--days N] [--out PATH] [--with-names] [--name NAME]`.

Note: the digest is written by a model and may name people in its prose; anonymizing replaces citation names only. The README says so.

- [ ] **Step 1: Write the failing tests**

`tests/test_report.py`:

```python
from pathlib import Path

from pulse.db import connect
from pulse.report import build_report
from pulse.run import main
from tests.test_cli import CONFIG
from tests.web_fakes import CONFIG as WEB_CONFIG, NOW, seed


def seeded(tmp_path):
    conn = connect(tmp_path / "pulse.db")
    seed(conn)
    with conn:
        conn.execute("INSERT INTO reactions (message_id, emoji, count) VALUES ('p1', '🎉', 6)")
    return conn


def test_report_is_self_contained(tmp_path):
    html = build_report(seeded(tmp_path), WEB_CONFIG, NOW, server_name="Acme")
    assert html.startswith("<!doctype html>") and "<style>" in html
    assert "<script" not in html and "http-equiv" not in html
    assert 'src="http' not in html and 'href="http' not in html.replace('href="https://discord.com/channels', "")
    for heading in ("Top pain points", "Coverage gaps", "Newcomers", "Community helpers", "Latest digest"):
        assert heading in html
    assert "Install fails on M1" in html and "Acme" in html


def test_report_anonymizes_by_default(tmp_path):
    conn = seeded(tmp_path)
    anon = build_report(conn, WEB_CONFIG, NOW)
    for name in ("@alice", "@uma", ">alice<", ">uma<", "carol"):
        assert name not in anon
    assert "<script" not in anon and 'class="av"' not in anon
    assert "@a member" in anon
    named = build_report(conn, WEB_CONFIG, NOW, with_names=True)
    assert "@uma" in named and "@alice" in named


def test_report_on_empty_db(tmp_path):
    html = build_report(connect(tmp_path / "pulse.db"), WEB_CONFIG, NOW)
    assert "No pain points" in html and "No newcomers" in html


def test_report_cli_writes_file(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "pulse.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "r.html"
    assert main(["--config", "pulse.toml", "report", "--days", "14", "--out", str(out), "--name", "Acme"]) == 0
    assert out.read_text().startswith("<!doctype html>") and str(out) in capsys.readouterr().out
    assert main(["--config", "pulse.toml", "report"]) == 0
    assert list(Path("reports").glob("pulse-*.html"))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_report.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.report'`

- [ ] **Step 3: Write the implementation**

`pulse/citations.py`: add a keyword-only `anonymize: bool = False` to `render_html`; in `anchor()` select `is_team` too and use

```python
        name = ("staff" if row["is_team"] else "a member") if anonymize else row["author_name"]
```

in place of `row["author_name"]` in the anchor text. Update the docstring's first sentence to mention it.

`pulse/report.py`:

```python
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
```

`pulse/web/templates/report.html`:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{{ server_name }} community pulse, last {{ days }} days</title>
<style>{{ css }}
.shared{max-width:1100px;margin:0 auto;padding:28px 20px;display:flex;flex-direction:column;gap:20px}</style>
</head>
<body>
<main class="shared">
  <div class="head"><div>
    <h1>{{ server_name }}: community pulse</h1>
    <p class="sub">Last {{ days }} days to {{ generated }} · {{ "names shown" if with_names else "authors anonymized" }} · made with Discord Pulse</p>
  </div></div>

  <div class="strip">
    <div class="stat"><span class="label">Community messages</span><span class="big">{{ cur.messages }}</span><span class="delta mut">{{ before.messages }} the {{ days }} days before</span></div>
    <div class="stat"><span class="label">Avg sentiment</span><span class="big">{{ cur.avg_sentiment|signed }}</span><span class="delta mut">{{ before.avg_sentiment|signed }} before</span></div>
    <div class="stat"><span class="label">Needed a reply</span><span class="big">{{ cur.needs_reply }}</span><span class="delta mut">{{ open_queue }} open in the mod queue</span></div>
    <div class="stat"><span class="label">Newcomers</span><span class="big">{{ nc.count }}</span><span class="delta mut">{{ nc.replied }} got a reply within 48h</span></div>
  </div>

  <section class="panel"><div class="panel-head"><h2>Sentiment and volume</h2></div>{{ chart }}</section>

  <section class="panel">
    <div class="panel-head"><h2>Top pain points</h2><span class="note">score = messages × negativity × growth</span></div>
    {% if pains %}
    <div class="tbl-wrap"><table class="chan">
      <thead><tr><th>Pain point</th><th>Status</th><th>Trend</th><th class="num">Last 7 days</th><th class="num">7 before</th><th class="num">Score</th></tr></thead>
      <tbody>{% for p in pains %}<tr><td>{{ p.name }}</td><td>{{ p.status }}</td><td>{{ p.spark }}</td><td class="num">{{ p.volume }}</td><td class="num">{{ p.prev }}</td><td class="num">{{ p.score|round|int }}</td></tr>{% endfor %}</tbody>
    </table></div>
    {% else %}<p class="empty">No pain points yet.</p>{% endif %}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Coverage gaps</h2><span class="note">questions vs staff messages by hour ({{ tz }})</span></div>
    {{ heat_questions }}
    {% if gaps %}<ul class="gaps">{% for g in gaps %}<li><b>{{ g.label }}</b>: {{ g.questions }} question{{ 's' if g.questions != 1 }}, {{ g.staff }} staff message{{ 's' if g.staff != 1 }}</li>{% endfor %}</ul>
    {% else %}<p class="empty">No questions in this window.</p>{% endif %}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Newcomers</h2></div>
    {% if nc.count %}<p>{{ nc.count }} new members posted for the first time; {{ nc.replied }} got a reply within 48 hours and {{ nc.returned }} came back.</p>
    {% else %}<p class="empty">No newcomers in this window.</p>{% endif %}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Community helpers</h2><span class="note">members answering other people's questions</span></div>
    {% if helpers %}<div class="tbl-wrap"><table class="chan">
      <thead><tr><th>Member</th><th class="num">Answers</th><th class="num">People helped</th></tr></thead>
      <tbody>{% for h in helpers %}<tr><td>{{ h.author_name }}</td><td class="num">{{ h.answers }}</td><td class="num">{{ h.helped }}</td></tr>{% endfor %}</tbody>
    </table></div>{% else %}<p class="empty">No member answers in this window.</p>{% endif %}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Most wanted</h2><span class="note">feature requests by reactions</span></div>
    {% for w in wanted %}<p class="mtext">{% for e, n in w.emojis %}<span class="pill kind">{{ e }} {{ n }}</span> {% endfor %}{{ w.content }} <span class="mut">({{ w.author }}, <a href="{{ w.link }}">open in Discord</a>)</span></p>
    {% else %}<p class="empty">No feature requests with reactions in this window.</p>{% endfor %}
  </section>

  <section class="panel">
    <div class="panel-head"><h2>Latest digest</h2></div>
    {% if digest_html %}<div class="prose">{{ digest_html }}</div>{% else %}<p class="empty">No digest yet.</p>{% endif %}
  </section>
</main>
</body>
</html>
```

`pulse/run.py`: add `"report"` to the commands that do not need provider keys (it is not in `MODEL_COMMANDS`), the parser

```python
    rep = sub.add_parser("report", help="write a single-file HTML report to share")
    rep.add_argument("--days", type=int, default=7)
    rep.add_argument("--out", type=Path, help="default: reports/pulse-<date>.html")
    rep.add_argument("--with-names", action="store_true", help="show author names and helper names")
    rep.add_argument("--name", help="server name shown in the title")
```

and the branch (import `build_report` from `pulse.report`):

```python
    elif args.command == "report":
        if args.days < 1:
            parser.error("--days must be at least 1")
        out = args.out or Path("reports") / f"pulse-{now:%Y-%m-%d}.html"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(build_report(conn, config, now, days=args.days, with_names=args.with_names,
                                    server_name=args.name or "Discord server"), encoding="utf-8")
        print(f"report written to {out}" + ("" if args.with_names else " (authors anonymized)"))
```

`.gitignore`: add `reports/`.

`README.md`, after the Dashboard section:

````markdown
## Shareable report

```bash
.venv/bin/python -m pulse.run report                  # last 7 days -> reports/pulse-<date>.html
.venv/bin/python -m pulse.run report --days 30 --name "Acme SDK" --out acme-september.html
```

One HTML file with everything inline (no scripts, nothing loaded from the internet): headline numbers, the sentiment chart, top pain points with their status, coverage gaps, newcomers, community helpers, most wanted and the latest digest. Authors are anonymized ("staff", "a member", "Helper 1") unless you add `--with-names`; message excerpts and Discord links stay. The digest is written by a model and may still mention people by name in its text. `reports/` is git-ignored.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_report.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/report.py pulse/web/templates/report.html pulse/citations.py pulse/run.py .gitignore README.md tests/test_report.py
git commit -m "feat: single-file shareable report with anonymized authors by default

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
