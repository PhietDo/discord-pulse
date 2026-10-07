import json

from pulse.agents.base import BackendResult, ProviderError
from pulse.agents.classifier import ChoiceResult
from pulse.agents.theme import ThemeStats, drop_unknown_assignments, run_themes, select_candidates
from pulse.db import connect
from pulse.pipeline import format_themes
from pulse.store import upsert_messages
from pulse.themes import assign, create_theme, mark_themed
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
    bad = proposal(assignments=[{"message_id": "ghost", "theme_ids": []}])
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


def bulk(n):
    conn = connect(":memory:")
    ids = [f"m{i:03d}" for i in range(n)]
    upsert_messages(conn, [msg(i, f"text {i}", minutes=k) for k, i in enumerate(ids)], frozenset())
    for i in ids:
        set_triage(conn, i, sentiment=-1, kind="bug", topics=["install"])
    return conn, ids


def six_new_themes(user):
    data = json.loads(user)
    ids = [m["message_id"] for m in data["messages"]]
    first = ids[0]
    return proposal(new_themes=[
        {"name": f"T {first} {k}", "description": "d", "message_ids": ids[k * 10:(k + 1) * 10]} for k in range(6)
    ])


def unthemed(conn):
    return {r[0] for r in conn.execute("SELECT message_id FROM triage WHERE themed_at IS NULL")}


def test_cap_rejected_new_theme_messages_stay_unthemed_for_next_run():
    conn, ids = bulk(120)
    backend = FakeBackend(handler=six_new_themes)
    stats = run_themes(conn, make_llm(conn, make_config(), backend), NOW)
    assert (stats.llm_batches, stats.created) == (2, 5)
    assert len(stats.rejected) == 7
    assigned = set(links(conn))
    assert len(assigned) == 50
    assert unthemed(conn) == set(ids) - assigned
    budgets = [(json.loads(c["user"])["new_themes_left"], json.loads(c["user"])["merges_left"]) for c in backend.calls]
    assert budgets == [(5, 3), (0, 3)]


def test_multi_batch_shares_budget_and_later_batches_see_new_themes():
    conn, ids = bulk(70)
    backend = FakeBackend(handler=all_messages_into_new_theme)
    stats = run_themes(conn, make_llm(conn, make_config(), backend), NOW)
    assert (stats.llm_batches, stats.created, stats.assigned) == (2, 1, 70)
    first, second = (json.loads(c["user"]) for c in backend.calls)
    assert first["themes"] == [] and first["new_themes_left"] == 5
    assert [t["name"] for t in second["themes"]] == ["Install failures"]
    assert second["new_themes_left"] == 4
    assert unthemed(conn) == set()


def test_daily_theme_limit_counts_earlier_runs_today():
    conn = seed(("a", ["install"], "u1"))
    with conn:
        for i in range(5):
            create_theme(conn, f"Earlier {i}", "", NOW.replace(hour=1))
    backend = FakeBackend(handler=all_messages_into_new_theme)
    stats = run_themes(conn, make_llm(conn, make_config(), backend), NOW)
    assert stats.created == 0
    assert any("5 new themes" in r for r in stats.rejected)
    assert json.loads(backend.calls[0]["user"])["new_themes_left"] == 0
    assert themed(conn) == set()


def test_jev_stage_stops_at_budget_cap_with_one_skipped_row():
    conn, ids = bulk(30)
    with conn:
        tid, _ = create_theme(conn, "Install failures", "wheels", NOW)
    fc = FakeClassifier(choose_handler=lambda s, q: ChoiceResult(str(tid), 0.95))
    config = make_config(classifier=classifier_config(), daily_usd_cap=0.0)
    stats = run_themes(conn, make_llm(conn, config, FakeBackend(), classifier=fc), NOW, concurrency=1)
    assert stats.skipped_budget is True
    rows = conn.execute("SELECT agent, status FROM agent_runs").fetchall()
    assert [tuple(r) for r in rows] == [("classifier", "skipped_budget")]
    assert fc.choose_calls == []


def test_jev_stage_mid_pool_cutoff_keeps_finished_assignments():
    conn, ids = bulk(10)
    with conn:
        tid, _ = create_theme(conn, "Install failures", "wheels", NOW)
    fc = FakeClassifier(choose_handler=lambda s, q: ChoiceResult(str(tid), 0.95, reported_cost=1.0))
    config = make_config(classifier=classifier_config(), daily_usd_cap=2.5)
    backend = FakeBackend()
    stats = run_themes(conn, make_llm(conn, config, backend, classifier=fc), NOW, concurrency=1)
    assert stats.skipped_budget is True and stats.jev_assigned == 3
    assert len(links(conn)) == 3 and len(themed(conn)) == 3
    assert [r[0] for r in conn.execute("SELECT status FROM agent_runs WHERE status != 'ok'")] == ["skipped_budget"]
    assert backend.calls == []


def test_jev_failures_are_counted_and_sent_to_llm():
    conn = seed(("a", ["install"], "u1"))
    with conn:
        create_theme(conn, "Install failures", "wheels", NOW)
    fc = FakeClassifier(choose_handler=lambda s, q: ProviderError("boom"))
    backend = FakeBackend(handler=lambda user: proposal())
    stats = run_themes(conn, make_llm(conn, make_config(classifier=classifier_config()), backend, classifier=fc), NOW)
    assert (stats.jev_failed, stats.llm_batches) == (1, 1)
    assert "jev failed 1" in format_themes(stats)
    assert "jev failed" not in format_themes(ThemeStats())


def test_jev_is_offered_the_most_recently_used_themes():
    conn, ids = bulk(2)
    with conn:
        tids = [create_theme(conn, f"T{i:02d}", "", NOW)[0] for i in range(42)]
        # m000 is older than m001; T41 gets the newest message, T40 the older one.
        assign(conn, "m001", tids[41])
        assign(conn, "m000", tids[40])
        mark_themed(conn, ids, NOW)
    upsert_messages(conn, [msg("new", "x", minutes=100)], frozenset())
    set_triage(conn, "new", sentiment=-1, kind="bug", topics=["install"])
    fc = FakeClassifier(choose_handler=lambda s, q: ChoiceResult("none", 0.9))
    backend = FakeBackend(handler=lambda user: proposal())
    run_themes(conn, make_llm(conn, make_config(classifier=classifier_config()), backend, classifier=fc), NOW)
    offered = [k for k in fc.choose_calls[0]["question"]["criteria"] if k != "none"]
    assert offered == [str(t) for t in [tids[41], tids[40], *tids[:38]]]


def test_invented_theme_ids_in_assignments_are_dropped_not_fatal():
    """Real models sometimes number their new themes and assign messages to those
    numbers. Those assignments are dropped (never attached to whatever theme later gets
    that id); messages only in such assignments stay unthemed for the next run."""
    conn = seed(("a", ["install"], "u1"), ("b", ["install"], "u2"), ("c", ["auth"], "u3"))
    response = proposal(
        new_themes=[{"name": "Install failures", "description": "wheels", "message_ids": ["a"]}],
        assignments=[{"message_id": "b", "theme_ids": [1]}, {"message_id": "c", "theme_ids": [2, 7]}],
    )
    stats = run_themes(conn, make_llm(conn, make_config(), FakeBackend(responses=[response])), NOW)
    assert (stats.failed_batches, stats.created, stats.assigned) == (0, 1, 1)
    assert links(conn) == {"a": 1}
    assert themed(conn) == {"a"}


def test_drop_unknown_assignments_lets_a_new_theme_claim_its_message():
    """A message can be assigned to a valid existing theme id and also be claimed by a new
    theme the model is proposing in the same reply. The new theme wins: the assignment is
    dropped (not kept) and the message is not reported as orphaned either."""
    data = {
        "assignments": [{"message_id": "a", "theme_ids": [1]}, {"message_id": "b", "theme_ids": [9]}],
        "new_themes": [{"name": "X", "description": "d", "message_ids": ["a"]}],
    }
    cleaned, orphaned = drop_unknown_assignments(data, {1})
    assert cleaned["assignments"] == []
    assert orphaned == {"b"}
