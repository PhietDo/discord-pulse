import json

from pulse.agents.base import BackendResult, ProviderError
from pulse.agents.classifier import QUESTIONS_VERSION
from pulse.agents.triage import run_triage
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import (
    FakeBackend, FakeClassifier, classifier_config, jev_result, make_config, make_llm, msg,
)

TEAM = frozenset({"t1"})


def llm_echo(user):
    items = json.loads(user)["messages"]
    return BackendResult({"results": [
        {"message_id": m["message_id"], "sentiment": -1, "confidence": 0.8, "kind": "bug",
         "topics": ["install"], "needs_reply": True}
        for m in items
    ]}, 100, 20)


def jev_by_content(state):
    text = state["content"]
    if "broken" in text:
        return jev_result(p=0.9, kind="bug", sentiment=-1)
    if "love" in text:
        return jev_result(p=0.05, kind="praise", sentiment=2)
    if "hmm" in text:
        return jev_result(p=0.2, kind="other", sentiment=0, kind_conf=0.4)
    if "known issue" in text:
        return jev_result(p=0.95, kind="bug", sentiment=-2)
    return jev_result(p=0.1, kind="other", sentiment=0)


def setup(messages, *, classifier=None, enabled=True, cap=5.0):
    conn = connect(":memory:")
    upsert_messages(conn, messages, TEAM)
    backend = FakeBackend(handler=llm_echo)
    fc = classifier or FakeClassifier(handler=jev_by_content)
    config = make_config(daily_usd_cap=cap, classifier=classifier_config(enabled=enabled))
    return conn, backend, fc, make_llm(conn, config, backend, classifier=fc)


def rows(conn):
    return {r["message_id"]: r for r in conn.execute("SELECT * FROM triage")}


def llm_ids(backend):
    return {m["message_id"] for c in backend.calls for m in json.loads(c["user"])["messages"]}


def test_confident_neutral_message_is_labeled_by_jev_only():
    conn, backend, fc, llm = setup([msg("m1", "good morning all")])
    stats = run_triage(conn, llm)
    r = rows(conn)["m1"]
    assert (r["labeler"], r["kind"], r["sentiment"], r["needs_reply"], json.loads(r["topics"])) == (
        "jev", "other", 0, 0, []
    )
    assert (r["needs_reply_p"], r["kind_confidence"], r["prompt_version"]) == (0.1, 0.9, QUESTIONS_VERSION)
    assert backend.calls == []
    assert (stats.triaged, stats.jev_labeled, stats.escalated) == (1, 1, 0)


def test_negative_message_escalates_to_llm_and_keeps_jev_probability():
    conn, backend, fc, llm = setup([msg("m1", "it's broken again"), msg("m2", "good morning", minutes=1)])
    stats = run_triage(conn, llm)
    assert llm_ids(backend) == {"m1"}
    r = rows(conn)
    assert (r["m1"]["labeler"], json.loads(r["m1"]["topics"]), r["m1"]["needs_reply_p"]) == ("llm", ["install"], 0.9)
    assert r["m2"]["labeler"] == "jev"
    assert (stats.triaged, stats.jev_labeled, stats.escalated) == (2, 1, 1)


def test_escalate_kinds_and_low_confidence_go_to_llm():
    conn, backend, fc, llm = setup([msg("p", "love the new cli"), msg("h", "hmm ok", minutes=1)])
    run_triage(conn, llm)
    assert llm_ids(backend) == {"p", "h"}


def test_staff_messages_are_overridden_and_never_escalated():
    conn, backend, fc, llm = setup([msg("s1", "known issue, fix ships today", author_id="t1")])
    run_triage(conn, llm)
    r = rows(conn)["s1"]
    assert (r["labeler"], r["sentiment"], r["kind"], r["needs_reply"], r["needs_reply_p"]) == ("jev", 0, "other", 0, 0.0)
    assert backend.calls == []


def test_classifier_failure_escalates_to_llm():
    fc = FakeClassifier(handler=lambda state: ProviderError("400"))
    conn, backend, fc, llm = setup([msg("m1", "good morning")], classifier=fc)
    stats = run_triage(conn, llm)
    assert llm_ids(backend) == {"m1"}
    assert stats.classifier_failed == 1
    r = rows(conn)["m1"]
    assert (r["labeler"], r["needs_reply_p"]) == ("llm", None)


def test_budget_hit_in_classifier_stage_stops_triage():
    conn, backend, fc, llm = setup([msg("m1", "it's broken"), msg("m2", "hi", minutes=1)], cap=0.0)
    stats = run_triage(conn, llm)
    assert rows(conn) == {}
    assert backend.calls == [] and fc.calls == []
    assert stats.skipped_budget_batches == 1
    assert stats.triaged == 0


def test_disabled_classifier_uses_llm_only():
    conn, backend, fc, llm = setup([msg("m1", "good morning")], enabled=False)
    run_triage(conn, llm)
    assert fc.calls == []
    assert llm_ids(backend) == {"m1"}
    assert rows(conn)["m1"]["labeler"] == "llm"


def test_escalated_messages_are_batched_newest_first():
    conn, backend, fc, llm = setup(
        [msg("old", "broken 1", minutes=0), msg("mid", "broken 2", minutes=1), msg("new", "broken 3", minutes=2)]
    )
    run_triage(conn, llm, batch_size=1, concurrency=1)
    first = json.loads(backend.calls[0]["user"])["messages"][0]["message_id"]
    assert first == "new"
