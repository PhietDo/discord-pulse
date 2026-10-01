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
    assert row["markdown"] == "Investigation failed: investigate: provider error: 401 bad key"
    assert json.loads(row["context"]) == {"theme_id": 1}


def test_empty_question_is_rejected():
    conn, _ = seed()
    with pytest.raises(ValueError):
        run_investigation(conn, make_llm(conn, make_config(), FakeBackend()), "  ", NOW)


def test_search_excludes_staff_messages():
    conn, tid = seed()
    # Add a team-member message that matches the search (t1 is a team member per make_config)
    upsert_messages(conn, [
        msg("s1", "Auth is broken for us too", minutes=7, author_id="t1", author_name="staff"),
    ], frozenset({"t1"}))
    set_triage(conn, "s1", sentiment=-1, kind="docs", topics=["auth docs"])
    box = Toolbox(conn, NOW)
    found = json.loads(box.execute(call("search_messages", text="auth")))
    # Should return t1, r1, q1 but NOT s1 (staff message)
    assert [m["message_id"] for m in found] == ["t1", "r1", "q1"]
    # s1 should not be in seen set
    assert "s1" not in box.seen
    assert box.seen == {"q1", "r1", "t1"}


def test_unexpected_error_marks_investigation_failed_and_reraises():
    conn, _ = seed()
    backend = FakeBackend(step_handler=lambda transcript, allow: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        run_investigation(conn, make_llm(conn, make_config(), backend), "why?", NOW)
    row = conn.execute("SELECT * FROM investigations").fetchone()
    assert row["markdown"].startswith("Investigation failed: RuntimeError:")


def test_get_thread_reports_bots():
    conn, _ = seed()
    upsert_messages(conn, [msg("b1", "auto-reply", minutes=8, reply_to_id="q1", is_bot=True)], frozenset())
    thread = json.loads(Toolbox(conn, NOW).execute(call("get_thread", message_id="q1")))
    assert {m["message_id"]: m["is_bot"] for m in thread} == {"q1": False, "r1": False, "t1": False, "b1": True}


def test_non_object_arguments_are_a_tool_error():
    conn, _ = seed()
    with pytest.raises(ToolError, match="arguments must be an object"):
        Toolbox(conn, NOW).execute(ToolCall("c", "search_messages", ["auth"]))


def test_search_text_wildcards_are_literal():
    conn, _ = seed()
    upsert_messages(conn, [msg("pc", "50% of builds fail", minutes=11)], frozenset())
    set_triage(conn, "pc", sentiment=-1, kind="bug")
    box = Toolbox(conn, NOW)
    assert [m["message_id"] for m in json.loads(box.execute(call("search_messages", text="%")))] == ["pc"]
    assert json.loads(box.execute(call("search_messages", text="a_th"))) == []
