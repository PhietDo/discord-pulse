import json
from datetime import timedelta

import pytest

from pulse.agents.base import BackendResult, BudgetExceeded, LLMError, ProviderError
from pulse.agents.digest import run_digest
from pulse.config import Launch
from pulse.db import connect
from pulse.modqueue import refresh_mod_queue
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
    # "before" is the 1.5 days just before the launch (equal to "after"), so the 9-day-old message is outside it.
    assert (data["before"]["messages"], data["before"]["days"], data["after"]["days"]) == (0, 1.5, 1.5)
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


def launch_db():
    conn = connect(":memory:")
    day = DAY0 = T0.replace(day=26, hour=0)  # launch day 2026-09-26, midnight UTC
    upsert_messages(conn, [
        msg("after", "v2 broke", minutes=(day - T0).total_seconds() / 60 + 60),
        msg("before", "v1 ok", minutes=(day - T0).total_seconds() / 60 - 2 * 24 * 60),
        msg("too_old", "v1 ok", minutes=(day - T0).total_seconds() / 60 - 5 * 24 * 60),
    ], frozenset())
    for mid in ("after", "before", "too_old"):
        set_triage(conn, mid, sentiment=-1, kind="bug", topics=["x"])
    sync_launches(conn, [Launch("v2.0 SDK", DAY0.date().isoformat(), ("v2",))])
    return conn, DAY0


def test_recent_launch_compares_equal_length_windows():
    conn, day = launch_db()
    backend = FakeBackend(handler=cite_first_and_bogus)
    run_digest(conn, make_llm(conn, make_config(), backend), day + timedelta(days=3), launch="v2.0 SDK")
    data = json.loads(backend.calls[0]["user"])
    assert (data["before"]["days"], data["after"]["days"]) == (3.0, 3.0)
    assert data["before"]["start"] == "2026-09-23T00:00:00.000000Z"
    assert (data["before"]["messages"], data["after"]["messages"]) == (1, 1)


def test_future_launch_raises_before_any_model_call():
    conn, day = launch_db()
    backend = FakeBackend(handler=cite_first_and_bogus)
    with pytest.raises(LookupError, match="in the future"):
        run_digest(conn, make_llm(conn, make_config(), backend), day - timedelta(hours=1), launch="v2.0 SDK")
    assert backend.calls == []
    assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


def test_launch_keywords_escape_like_wildcards():
    conn = seed()
    sync_launches(conn, [Launch("pct", "2026-09-28", ("v_",))])
    backend = FakeBackend(handler=cite_first_and_bogus)
    run_digest(conn, make_llm(conn, make_config(), backend), NOW, launch="pct")
    # "v_" must not act as a wildcard matching "v2"; only sampled messages remain.
    data = json.loads(backend.calls[0]["user"])
    assert [m["message_id"] for m in data["messages"]][0] == "love"


def test_weekly_digest_includes_open_queue_items_first_with_reason():
    conn = seed()
    refresh_mod_queue(conn, make_config(), NOW)
    backend = FakeBackend(handler=lambda user: BackendResult(
        {"markdown": "## Needs attention\nReply here [[msg:bug1]]."}, 10, 10))
    result = run_digest(conn, make_llm(conn, make_config(), backend), NOW)
    messages = json.loads(backend.calls[0]["user"])["messages"]
    assert messages[0]["message_id"] == "bug1" and messages[0]["queue_reason"] == "frustrated"
    assert all("queue_reason" not in m for m in messages[1:])
    assert [m["message_id"] for m in messages].count("bug1") == 1
    assert result.cited_message_ids == ["bug1"]


def test_empty_window_skips_the_model():
    conn = seed()
    backend = FakeBackend()
    result = run_digest(conn, make_llm(conn, make_config(), backend), NOW + timedelta(days=30))
    assert backend.calls == []
    assert result.markdown == "_No community activity in this period._"
    row = conn.execute("SELECT markdown, run_id FROM digests").fetchone()
    assert tuple(row) == ("_No community activity in this period._", None)
    assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0


@pytest.mark.parametrize("cap, outcome, error", [
    (0.0, None, BudgetExceeded),
    (5.0, ProviderError("400"), LLMError),
])
def test_failed_digest_saves_no_row(cap, outcome, error):
    conn = seed()
    backend = FakeBackend(responses=[outcome] if outcome else [])
    with pytest.raises(error):
        run_digest(conn, make_llm(conn, make_config(daily_usd_cap=cap), backend), NOW)
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 0


def test_digest_stores_removed_citations():
    conn = seed()
    result = run_digest(conn, make_llm(conn, make_config(), FakeBackend(handler=cite_first_and_bogus)), NOW)
    row = conn.execute("SELECT removed_citations FROM digests WHERE id = ?", (result.digest_id,)).fetchone()
    assert json.loads(row["removed_citations"]) == ["bogus"]
