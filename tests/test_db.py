import sqlite3

import pytest

from pulse.db import connect

EXPECTED_TABLES = {
    "messages", "triage", "themes", "message_themes", "theme_events", "launches",
    "mod_queue", "digests", "investigations", "agent_runs",
}


def test_connect_creates_all_tables():
    conn = connect(":memory:")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "p.db"
    connect(path).close()
    conn = connect(path)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_foreign_keys_enforced():
    conn = connect(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO triage (message_id, sentiment, confidence, kind, topics, needs_reply,"
            " prompt_version, created_at) VALUES ('nope', 0, 1.0, 'other', '[]', 0, 'v', 'x')"
        )


def test_sentiment_range_enforced():
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content,"
        " created_at, source) VALUES ('m', 'g', 'c', 'a', 'n', 'hi', 'x', 'file')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO triage (message_id, sentiment, confidence, kind, topics, needs_reply,"
            " prompt_version, created_at) VALUES ('m', 3, 1.0, 'other', '[]', 0, 'v', 'x')"
        )
