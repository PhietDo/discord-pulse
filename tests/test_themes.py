import json

from pulse.db import connect
from pulse.store import upsert_messages
from pulse.themes import (
    ThemeBudget, active_themes, apply_proposal, assign, create_theme, find_active, mark_themed, merge_themes,
    rename_theme,
)
from tests.fakes import T0, msg, set_triage


def db(*ids):
    conn = connect(":memory:")
    upsert_messages(conn, [msg(i, minutes=n) for n, i in enumerate(ids)], frozenset())
    for i in ids:
        set_triage(conn, i, sentiment=-1, kind="bug", topics=["install"])
    return conn


def events(conn):
    return [(r["kind"], json.loads(r["payload"])) for r in conn.execute("SELECT * FROM theme_events ORDER BY id")]


def empty(**overrides):
    base = {"assignments": [], "new_themes": [], "merges": [], "renames": []}
    base.update(overrides)
    return base


def test_create_theme_dedupes_case_insensitively():
    conn = db()
    with conn:
        tid, created = create_theme(conn, "Auth  Docs", "confusing auth guide", T0)
        again, created_again = create_theme(conn, "auth docs", "other", T0)
    assert created and not created_again and again == tid
    assert find_active(conn, " AUTH docs ") == tid
    assert [k for k, _ in events(conn)] == ["create"]


def test_merge_and_rename_log_events():
    conn = db()
    with conn:
        a, _ = create_theme(conn, "A", "a", T0)
        b, _ = create_theme(conn, "B", "b", T0)
        merge_themes(conn, b, a, T0, run_id=None, reason="same thing")
        rename_theme(conn, a, "A2", "a2", T0)
    assert [r["id"] for r in active_themes(conn)] == [a]
    assert tuple(conn.execute("SELECT status, merged_into FROM themes WHERE id = ?", (b,)).fetchone()) == ("merged", a)
    kinds = [k for k, _ in events(conn)]
    assert kinds == ["create", "create", "merge", "rename"]
    assert events(conn)[3][1] == {"theme_id": a, "old_name": "A", "name": "A2", "old_description": "a", "description": "a2"}


def test_assign_is_idempotent_and_mark_themed_sets_timestamp():
    conn = db("m1")
    with conn:
        tid, _ = create_theme(conn, "A", "a", T0)
        assert assign(conn, "m1", tid) is True
        assert assign(conn, "m1", tid) is False
        mark_themed(conn, ["m1"], T0)
    assert conn.execute("SELECT themed_at FROM triage WHERE message_id = 'm1'").fetchone()[0] == "2026-09-28T12:00:00.000000Z"


def test_apply_proposal_full_flow():
    conn = db("m1", "m2", "m3")
    with conn:
        old, _ = create_theme(conn, "Old name", "x", T0)
        dup, _ = create_theme(conn, "Dup", "y", T0)
        changes = apply_proposal(conn, {
            "renames": [{"theme_id": old, "name": "M1 install", "description": "arm64 wheels"}],
            "merges": [{"from_id": dup, "into_id": old, "reason": "same issue"}],
            "new_themes": [{"name": "Auth docs", "description": "token step missing", "message_ids": ["m2"]}],
            "assignments": [{"message_id": "m1", "theme_ids": [old]}, {"message_id": "m3", "theme_ids": [dup]}],
        }, T0, None, ThemeBudget())
    assert (changes.renamed, changes.merged, changes.created, changes.assigned, changes.rejected) == (1, 1, 1, 3, [])
    by_msg = dict(conn.execute("SELECT message_id, theme_id FROM message_themes").fetchall())
    assert by_msg["m1"] == old and by_msg["m3"] == old  # m3 went to the merge target
    assert conn.execute("SELECT name FROM themes WHERE id = ?", (by_msg["m2"],)).fetchone()[0] == "Auth docs"


def test_apply_proposal_enforces_run_limits():
    conn = db("m1")
    budget = ThemeBudget()
    with conn:
        ids = [create_theme(conn, f"T{i}", "", T0)[0] for i in range(5)]
        changes = apply_proposal(conn, empty(
            new_themes=[{"name": f"New {i}", "description": "", "message_ids": []} for i in range(6)],
            merges=[{"from_id": ids[i], "into_id": ids[4], "reason": ""} for i in range(4)],
        ), T0, None, budget)
    assert (changes.created, changes.merged) == (5, 3)
    assert len(changes.rejected) == 2
    assert any("5 new themes" in r for r in changes.rejected)
    assert any("3 merges" in r for r in changes.rejected)
    assert (budget.new_themes_left, budget.merges_left) == (0, 0)


def test_new_theme_matching_existing_name_reuses_it_without_budget():
    conn = db("m1")
    budget = ThemeBudget(new_themes_left=0)
    with conn:
        tid, _ = create_theme(conn, "Rate limits", "", T0)
        changes = apply_proposal(conn, empty(
            new_themes=[{"name": "rate LIMITS", "description": "", "message_ids": ["m1"]}]
        ), T0, None, budget)
    assert (changes.created, changes.assigned, changes.rejected) == (0, 1, [])
    assert conn.execute("SELECT theme_id FROM message_themes").fetchone()[0] == tid


def test_invalid_references_are_rejected_not_applied():
    conn = db("m1")
    with conn:
        tid, _ = create_theme(conn, "A", "", T0)
        changes = apply_proposal(conn, empty(
            renames=[{"theme_id": 999, "name": "X", "description": ""}],
            merges=[{"from_id": tid, "into_id": tid, "reason": ""}],
            assignments=[{"message_id": "m1", "theme_ids": [999]}],
        ), T0, None, ThemeBudget())
    assert (changes.renamed, changes.merged, changes.assigned) == (0, 0, 0)
    assert len(changes.rejected) == 3
    assert conn.execute("SELECT COUNT(*) FROM message_themes").fetchone()[0] == 0
