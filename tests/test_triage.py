import json

from pulse.agents.base import BackendResult
from pulse.agents.triage import MAX_CONTENT_CHARS, PROMPT_VERSION, run_triage
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import T0, FakeBackend, make_config, make_llm, msg

TEAM = frozenset({"t1"})


def echo(user, drop=None, sentiment=-1):
    items = json.loads(user)["messages"]
    return BackendResult(
        {"results": [
            {"message_id": m["message_id"], "sentiment": sentiment, "confidence": 0.8, "kind": "bug",
             "topics": ["Install ", ""], "needs_reply": True}
            for m in items if m["message_id"] != drop
        ]},
        100, 20,
    )


def setup(messages, **config_overrides):
    conn = connect(":memory:")
    upsert_messages(conn, messages, TEAM)
    backend = FakeBackend(handler=echo)
    llm = make_llm(conn, make_config(**config_overrides), backend)
    return conn, backend, llm


def triage_rows(conn):
    return {r["message_id"]: r for r in conn.execute("SELECT * FROM triage")}


def payload_items(call):
    return {m["message_id"]: m for m in json.loads(call["user"])["messages"]}


def test_triages_eligible_messages_and_skips_bots_and_empty():
    conn, backend, llm = setup([msg("m1", "broken"), msg("m2", "   "), msg("m3", "beep", is_bot=True)])
    stats = run_triage(conn, llm)
    assert stats.triaged == 1
    row = triage_rows(conn)["m1"]
    assert (row["sentiment"], row["kind"], row["needs_reply"]) == (-1, "bug", 1)
    assert json.loads(row["topics"]) == ["install"]
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["run_id"] is not None
    assert backend.calls[0]["schema_name"] == "triage_result"


def test_splits_into_batches():
    conn, backend, llm = setup([msg(f"m{i}", minutes=i) for i in range(5)])
    stats = run_triage(conn, llm, batch_size=2)
    assert stats.triaged == 5
    assert len(backend.calls) == 3


def test_payload_includes_reply_parent_and_preceding_context():
    conn, backend, llm = setup([
        msg("m1", "first", minutes=0),
        msg("m2", "second", minutes=1, author_id="t1", author_name="staffer"),
        msg("m3", "still broken?", minutes=2, reply_to_id="m1"),
        msg("x1", "other channel", minutes=1, channel_id="999"),
    ])
    run_triage(conn, llm)
    item = payload_items(backend.calls[0])["m3"]
    assert item["reply_to"] == {"author": "alice", "content": "first"}
    assert [c["content"] for c in item["context"]] == ["first", "second"]
    assert payload_items(backend.calls[0])["m2"]["is_team"] is True


def test_long_content_is_truncated_in_payload():
    conn, backend, llm = setup([msg("m1", "x" * 2500)])
    run_triage(conn, llm)
    assert len(payload_items(backend.calls[0])["m1"]["content"]) == MAX_CONTENT_CHARS + 1


def test_batch_with_missing_ids_fails_without_storing():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1"), msg("m2", minutes=1), msg("m3", minutes=2)], TEAM)
    backend = FakeBackend(handler=lambda user: echo(user, drop="m2"))
    stats = run_triage(conn, make_llm(conn, make_config(), backend), batch_size=1)
    assert stats.triaged == 2
    assert stats.failed_batches == 1
    assert set(triage_rows(conn)) == {"m1", "m3"}
    failed = conn.execute("SELECT error FROM agent_runs WHERE status = 'failed'").fetchone()
    assert "m2" in failed["error"]


def test_budget_cap_skips_all_batches():
    conn, backend, llm = setup([msg("m1"), msg("m2", minutes=1)], daily_usd_cap=0.0)
    stats = run_triage(conn, llm, batch_size=1)
    assert stats.skipped_budget_batches == 2
    assert stats.triaged == 0
    assert backend.calls == []


def test_second_run_is_a_no_op():
    conn, backend, llm = setup([msg("m1")])
    run_triage(conn, llm)
    stats = run_triage(conn, llm)
    assert stats.triaged == 0
    assert len(backend.calls) == 1


def test_force_since_retriages_only_the_range():
    conn, backend, llm = setup([msg("old", minutes=0), msg("new", minutes=120)])
    run_triage(conn, llm)
    stats = run_triage(conn, llm, since=T0.replace(hour=13), force=True)
    assert stats.triaged == 1
    assert list(payload_items(backend.calls[-1])) == ["new"]
    assert set(triage_rows(conn)) == {"old", "new"}
