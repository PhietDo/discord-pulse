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
