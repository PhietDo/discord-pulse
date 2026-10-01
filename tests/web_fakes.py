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
