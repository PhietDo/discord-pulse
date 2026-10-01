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
