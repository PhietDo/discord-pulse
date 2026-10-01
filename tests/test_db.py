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


def test_wal_mode_enabled_for_file_db(tmp_path):
    conn = connect(tmp_path / "p.db")
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_busy_timeout_set(tmp_path):
    conn = connect(tmp_path / "p.db")
    timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    assert timeout == 5000


OLD_SCHEMA = """
CREATE TABLE messages (id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
  channel_name TEXT NOT NULL DEFAULT '', thread_id TEXT, author_id TEXT NOT NULL, author_name TEXT NOT NULL,
  author_avatar_url TEXT, is_team INTEGER NOT NULL DEFAULT 0, is_bot INTEGER NOT NULL DEFAULT 0,
  content TEXT NOT NULL, created_at TEXT NOT NULL, edited_at TEXT, reply_to_id TEXT, source TEXT NOT NULL);
CREATE TABLE triage (message_id TEXT PRIMARY KEY, sentiment INTEGER NOT NULL, confidence REAL NOT NULL,
  kind TEXT NOT NULL, topics TEXT NOT NULL, needs_reply INTEGER NOT NULL, prompt_version TEXT NOT NULL,
  run_id INTEGER, created_at TEXT NOT NULL);
INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at, source)
  VALUES ('m', 'g', 'c', 'a', 'n', 'hi', 'x', 'file');
INSERT INTO triage VALUES ('m', -1, 0.9, 'bug', '[]', 1, 'triage-v2', NULL, 'x');
"""


def triage_columns(conn):
    return {r["name"] for r in conn.execute("PRAGMA table_info(triage)")}


def test_fresh_db_has_classifier_columns():
    assert {"needs_reply_p", "kind_confidence", "labeler"} <= triage_columns(connect(":memory:"))


def test_old_triage_table_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()

    conn = connect(path)

    assert {"needs_reply_p", "kind_confidence", "labeler"} <= triage_columns(conn)
    row = conn.execute("SELECT * FROM triage WHERE message_id = 'm'").fetchone()
    assert (row["sentiment"], row["kind"], row["labeler"], row["needs_reply_p"], row["kind_confidence"]) == (
        -1, "bug", "llm", None, None
    )


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "p.db"
    connect(path).close()
    connect(path).close()
    assert "labeler" in triage_columns(connect(path))


def test_triage_has_themed_at_column():
    assert "themed_at" in triage_columns(connect(":memory:"))


def test_old_db_gains_themed_at(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()
    conn = connect(path)
    assert "themed_at" in triage_columns(conn)
    assert conn.execute("SELECT themed_at FROM triage WHERE message_id = 'm'").fetchone()[0] is None


def test_old_messages_table_gains_parent_channel_id(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(
        "CREATE TABLE messages (id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,"
        " channel_name TEXT NOT NULL DEFAULT '', thread_id TEXT, author_id TEXT NOT NULL,"
        " author_name TEXT NOT NULL, author_avatar_url TEXT, is_team INTEGER NOT NULL DEFAULT 0,"
        " is_bot INTEGER NOT NULL DEFAULT 0, content TEXT NOT NULL, created_at TEXT NOT NULL,"
        " edited_at TEXT, reply_to_id TEXT, source TEXT NOT NULL);"
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at, source)"
        " VALUES ('m1', '900', '100', 'u1', 'alice', 'hi', '2026-09-28T12:00:00.000000Z', 'file');"
    )
    raw.commit()
    raw.close()
    conn = connect(path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
    assert "parent_channel_id" in cols
    assert conn.execute("SELECT parent_channel_id FROM messages WHERE id = 'm1'").fetchone()[0] is None
    conn.close()
    connect(path).close()  # migrating twice is a no-op
