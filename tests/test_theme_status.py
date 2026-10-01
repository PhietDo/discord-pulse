from datetime import timedelta

import pytest

from pulse import theme_status as ts
from pulse.db import connect
from pulse.models import to_iso
from pulse.store import upsert_messages
from tests.fakes import T0, msg, set_triage


def _db():
    conn = connect(":memory:")
    with conn:
        conn.execute("INSERT INTO themes (id, name, created_at) VALUES (1, 'auth docs', ?)", (to_iso(T0),))
        conn.execute("INSERT INTO themes (id, name, created_at) VALUES (2, 'login', ?)", (to_iso(T0),))
    return conn


def test_set_and_read_status():
    conn = _db()
    ts.set_status(conn, 1, "acknowledged", "  docs team rewriting  ", T0)
    assert ts.status_for(conn, 1) == {
        "status": "acknowledged", "label": "Acknowledged", "note": "docs team rewriting",
        "updated_at": to_iso(T0), "shipped_at": None,
    }
    assert ts.status_for(conn, 2)["status"] == "new"
    assert ts.status_for(conn, 2)["label"] == "Not triaged"
    assert set(ts.statuses(conn)) == {1}


def test_rejects_unknown_status_and_theme():
    conn = _db()
    with pytest.raises(ValueError):
        ts.set_status(conn, 1, "done", "", T0)
    with pytest.raises(LookupError):
        ts.set_status(conn, 99, "acknowledged", "", T0)


def test_note_is_capped():
    conn = _db()
    ts.set_status(conn, 1, "acknowledged", "x" * 900, T0)
    assert len(ts.status_for(conn, 1)["note"]) == ts.NOTE_MAX


def test_status_moves_to_merge_target_newest_wins():
    conn = _db()
    ts.set_status(conn, 2, "in_progress", "fixing login", T0)
    ts.set_status(conn, 1, "acknowledged", "older", T0 - timedelta(hours=1))
    with conn:
        conn.execute("UPDATE themes SET status = 'merged', merged_into = 1 WHERE id = 2")
    assert ts.status_for(conn, 1)["status"] == "in_progress"
    assert ts.status_for(conn, 2)["status"] == "in_progress"


def test_shipped_at_is_kept_while_shipped_and_cleared_after():
    conn = _db()
    ts.set_status(conn, 1, "shipped", "2.0.1", T0)
    ts.set_status(conn, 1, "shipped", "2.0.1, docs updated", T0 + timedelta(hours=2))
    assert ts.status_for(conn, 1)["shipped_at"] == to_iso(T0)
    ts.set_status(conn, 1, "in_progress", "regressed", T0 + timedelta(hours=3))
    assert ts.status_for(conn, 1)["shipped_at"] is None


def test_shipped_comparison_uses_equal_windows():
    conn = _db()
    rows = [msg(f"b{i}", minutes=-60 * (i + 1)) for i in range(3)] + [msg("a0", minutes=60)]
    rows.append(msg("old", minutes=-60 * 24 * 5))  # outside the before window
    upsert_messages(conn, rows, frozenset())
    for m in rows:
        set_triage(conn, m.id, sentiment=-2 if m.id.startswith("b") else 1)
        with conn:
            conn.execute("INSERT INTO message_themes (message_id, theme_id) VALUES (?, 1)", (m.id,))
    assert ts.shipped_comparison(conn, 1, T0) is None
    ts.set_status(conn, 1, "shipped", "", T0)
    now = T0 + timedelta(days=2)
    c = ts.shipped_comparison(conn, 1, now)
    assert c["days"] == 2.0
    assert c["shipped_at"] == to_iso(T0)
    assert c["before"] == {"messages": 3, "avg_sentiment": -2.0}
    assert c["after"] == {"messages": 1, "avg_sentiment": 1.0}
