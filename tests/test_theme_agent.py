import json

from pulse.agents.base import BackendResult
from pulse.agents.classifier import ChoiceResult
from pulse.agents.theme import run_themes, select_candidates
from pulse.db import connect
from pulse.store import upsert_messages
from pulse.themes import create_theme
from tests.fakes import (
    T0, FakeBackend, FakeClassifier, classifier_config, make_config, make_llm, msg, set_triage,
)

NOW = T0


def seed(*specs):
    """specs: (id, topics, author_id)"""
    conn = connect(":memory:")
    upsert_messages(conn, [msg(i, f"text {i}", minutes=n, author_id=a) for n, (i, _, a) in enumerate(specs)],
                    frozenset({"t1"}))
    for i, topics, _ in specs:
        set_triage(conn, i, sentiment=-1, kind="bug", topics=topics)
    return conn


def proposal(**overrides):
    base = {"assignments": [], "new_themes": [], "merges": [], "renames": []}
    base.update(overrides)
    return BackendResult(base, 100, 20)


def all_messages_into_new_theme(user):
    ids = [m["message_id"] for m in json.loads(user)["messages"]]
    return proposal(new_themes=[{"name": "Install failures", "description": "wheels", "message_ids": ids}])


def themed(conn):
    return {r[0] for r in conn.execute("SELECT message_id FROM triage WHERE themed_at IS NOT NULL")}


def links(conn):
    return dict(conn.execute("SELECT message_id, theme_id FROM message_themes").fetchall())


def test_candidates_need_topics_and_exclude_staff():
    conn = seed(("a", ["install"], "u1"), ("b", [], "u1"), ("s", ["install"], "t1"))
    assert [r["id"] for r in select_candidates(conn)] == ["a"]


def test_llm_creates_first_theme_and_marks_batch_themed():
    conn = seed(("a", ["install"], "u1"), ("b", ["install"], "u2"))
    backend = FakeBackend(handler=all_messages_into_new_theme)
    stats = run_themes(conn, make_llm(conn, make_config(), backend), NOW)
    assert (stats.considered, stats.llm_batches, stats.created, stats.assigned) == (2, 1, 1, 2)
    assert set(links(conn)) == {"a", "b"}
    assert themed(conn) == {"a", "b"}
    assert backend.calls[0]["schema_name"] == "theme_result"
    assert json.loads(backend.calls[0]["user"])["themes"] == []


def test_jev_assigns_confident_matches_and_llm_gets_leftovers():
    conn = seed(("a", ["install"], "u1"), ("b", ["auth"], "u2"))
    with conn:
        tid, _ = create_theme(conn, "Install failures", "wheels", NOW)

    def pick(state, question):
        return ChoiceResult(str(tid), 0.95) if state["message_id"] == "a" else ChoiceResult("none", 0.9)

    fc = FakeClassifier(choose_handler=pick)
    backend = FakeBackend(handler=lambda user: proposal())
    config = make_config(classifier=classifier_config())
    stats = run_themes(conn, make_llm(conn, config, backend, classifier=fc), NOW)
    assert stats.jev_assigned == 1
    assert links(conn) == {"a": tid}
    assert [m["message_id"] for m in json.loads(backend.calls[0]["user"])["messages"]] == ["b"]
    assert themed(conn) == {"a", "b"}


def test_low_confidence_jev_choice_goes_to_llm():
    conn = seed(("a", ["install"], "u1"))
    with conn:
        tid, _ = create_theme(conn, "Install failures", "wheels", NOW)
    fc = FakeClassifier(choices=[ChoiceResult(str(tid), 0.3)])
    backend = FakeBackend(handler=lambda user: proposal(assignments=[{"message_id": "a", "theme_ids": [tid]}]))
    stats = run_themes(conn, make_llm(conn, make_config(classifier=classifier_config()), backend, classifier=fc), NOW)
    assert (stats.jev_assigned, stats.llm_batches, stats.assigned) == (0, 1, 1)


def test_invalid_proposal_is_retried_then_left_unthemed():
    conn = seed(("a", ["install"], "u1"))
    bad = proposal(assignments=[{"message_id": "a", "theme_ids": [999]}])
    backend = FakeBackend(responses=[bad, bad])
    stats = run_themes(conn, make_llm(conn, make_config(), backend), NOW)
    assert (stats.failed_batches, stats.assigned) == (1, 0)
    assert themed(conn) == set()
    assert len(backend.calls) == 2


def test_budget_stop_leaves_messages_for_next_run():
    conn = seed(("a", ["install"], "u1"))
    backend = FakeBackend(handler=all_messages_into_new_theme)
    stats = run_themes(conn, make_llm(conn, make_config(daily_usd_cap=0.0), backend), NOW)
    assert stats.skipped_budget is True
    assert backend.calls == [] and themed(conn) == set()


def test_second_run_is_a_no_op():
    conn = seed(("a", ["install"], "u1"))
    backend = FakeBackend(handler=all_messages_into_new_theme)
    llm = make_llm(conn, make_config(), backend)
    run_themes(conn, llm, NOW)
    stats = run_themes(conn, llm, NOW)
    assert stats.considered == 0 and len(backend.calls) == 1
