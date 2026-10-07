import json
from datetime import timedelta

from pulse.agents.base import BackendResult, ProviderError
from pulse.agents.classifier import QUESTIONS_VERSION
from pulse.agents.triage import run_triage
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import (
    T0, FakeBackend, FakeClassifier, classifier_config, jev_result, make_config, make_llm, msg,
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


def test_staff_messages_skip_the_classifier_and_the_llm():
    conn, backend, fc, llm = setup([msg("s1", "known issue, fix ships today", author_id="t1")])
    stats = run_triage(conn, llm)
    r = rows(conn)["s1"]
    assert (r["labeler"], r["sentiment"], r["kind"], r["needs_reply"], r["needs_reply_p"]) == ("rule", 0, "other", 0, 0.0)
    assert r["prompt_version"] == "staff-rule"
    assert backend.calls == [] and fc.calls == []
    assert stats.staff_rule == 1


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


def test_classifier_failures_are_not_counted_as_escalations():
    fc = FakeClassifier(handler=lambda state: ProviderError("400"))
    conn, backend, fc, llm = setup(
        [msg("m1", "good morning"), msg("m2", "good evening", minutes=1)], classifier=fc
    )
    stats = run_triage(conn, llm)
    assert stats.classifier_failed == 2
    assert stats.escalated == 0
    assert llm_ids(backend) == {"m1", "m2"}


def test_disabled_classifier_uses_llm_only():
    conn, backend, fc, llm = setup([msg("m1", "good morning")], enabled=False)
    run_triage(conn, llm)
    assert fc.calls == []
    assert llm_ids(backend) == {"m1"}
    assert rows(conn)["m1"]["labeler"] == "llm"


def test_force_retriage_keeps_existing_llm_topics():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", "good morning")], TEAM)
    backend = FakeBackend(handler=llm_echo)
    fc = FakeClassifier(handler=jev_by_content)

    disabled_config = make_config(classifier=classifier_config(enabled=False))
    llm_disabled = make_llm(conn, disabled_config, backend, classifier=fc)
    run_triage(conn, llm_disabled)
    first = rows(conn)["m1"]
    assert (first["labeler"], json.loads(first["topics"])) == ("llm", ["install"])
    calls_before = len(backend.calls)

    enabled_config = make_config(classifier=classifier_config(enabled=True))
    llm_enabled = make_llm(conn, enabled_config, backend, classifier=fc)
    stats = run_triage(conn, llm_enabled, since=T0 - timedelta(days=1), force=True)

    r = rows(conn)["m1"]
    assert (r["labeler"], json.loads(r["topics"])) == ("llm", ["install"])
    assert stats.kept_llm == 1
    assert len(backend.calls) == calls_before


def test_budget_cutoff_mid_stage_a_keeps_finished_rows():
    conn = connect(":memory:")
    messages = [msg(f"m{i}", "good morning", minutes=i) for i in range(4)]
    upsert_messages(conn, messages, TEAM)
    backend = FakeBackend(handler=llm_echo)
    fc = FakeClassifier(handler=lambda state: jev_result(cost=0.00002))
    config = make_config(daily_usd_cap=0.00004, classifier=classifier_config(enabled=True))
    llm = make_llm(conn, config, backend, classifier=fc)

    stats = run_triage(conn, llm, classify_concurrency=1)

    written = rows(conn)
    assert len(written) == 2
    assert all(r["labeler"] == "jev" for r in written.values())
    assert backend.calls == []
    assert stats.left_untriaged == 2
    statuses = [r["status"] for r in conn.execute("SELECT status FROM agent_runs WHERE agent = 'classifier'")]
    assert "skipped_budget" in statuses


def test_escalated_messages_are_batched_newest_first():
    conn, backend, fc, llm = setup(
        [msg("old", "broken 1", minutes=0), msg("mid", "broken 2", minutes=1), msg("new", "broken 3", minutes=2)]
    )
    run_triage(conn, llm, batch_size=1, concurrency=1)
    first = json.loads(backend.calls[0]["user"])["messages"][0]["message_id"]
    assert first == "new"
