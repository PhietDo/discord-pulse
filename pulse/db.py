"""SQLite schema and connection."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    thread_id TEXT,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    author_avatar_url TEXT,
    is_team INTEGER NOT NULL DEFAULT 0,
    is_bot INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edited_at TEXT,
    reply_to_id TEXT,
    source TEXT NOT NULL,
    parent_channel_id TEXT,
    parent_channel_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages(reply_to_id);
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id);

CREATE TABLE IF NOT EXISTS reactions (
    message_id TEXT NOT NULL REFERENCES messages(id),
    emoji TEXT NOT NULL,
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (message_id, emoji)
);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'skipped_budget')),
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at);

CREATE TABLE IF NOT EXISTS triage (
    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    sentiment INTEGER NOT NULL CHECK (sentiment BETWEEN -2 AND 2),
    confidence REAL NOT NULL,
    kind TEXT NOT NULL,
    topics TEXT NOT NULL,
    needs_reply INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL,
    needs_reply_p REAL,
    kind_confidence REAL,
    labeler TEXT NOT NULL DEFAULT 'llm',
    themed_at TEXT
);

CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'merged')),
    merged_into INTEGER REFERENCES themes(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS theme_status (
    theme_id INTEGER PRIMARY KEY REFERENCES themes(id),
    status TEXT NOT NULL CHECK (status IN ('new', 'acknowledged', 'in_progress', 'shipped')),
    note TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    shipped_at TEXT
);

CREATE TABLE IF NOT EXISTS theme_issues (
    theme_id INTEGER NOT NULL REFERENCES themes(id),
    tracker TEXT NOT NULL CHECK (tracker IN ('github', 'linear')),
    url TEXT NOT NULL,
    identifier TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (theme_id, tracker)
);

CREATE TABLE IF NOT EXISTS message_themes (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    theme_id INTEGER NOT NULL REFERENCES themes(id),
    PRIMARY KEY (message_id, theme_id)
);
CREATE INDEX IF NOT EXISTS idx_message_themes_theme ON message_themes(theme_id);

CREATE TABLE IF NOT EXISTS theme_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'rename', 'merge')),
    payload TEXT NOT NULL,
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    date TEXT NOT NULL,
    keywords TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mod_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_key TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id),
    thread_id TEXT,
    reason TEXT NOT NULL CHECK (reason IN ('unanswered', 'frustrated')),
    status TEXT NOT NULL CHECK (status IN ('open', 'handled', 'dismissed')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_mod_queue_key ON mod_queue(queue_key, status);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('weekly', 'launch')),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    launch_id INTEGER REFERENCES launches(id),
    markdown TEXT NOT NULL,
    cited_message_ids TEXT NOT NULL,
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL,
    removed_citations TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS alerts_sent (
    kind TEXT NOT NULL CHECK (kind IN ('spike', 'frustrated')),
    key TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    context TEXT NOT NULL,
    markdown TEXT,
    cited_message_ids TEXT NOT NULL DEFAULT '[]',
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL,
    removed_citations TEXT NOT NULL DEFAULT '[]'
);
"""

# Columns added after a table was first created. CREATE TABLE IF NOT EXISTS leaves an
# existing table untouched, so databases created earlier get them via ALTER TABLE.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "triage": (
        ("needs_reply_p", "REAL"),
        ("kind_confidence", "REAL"),
        ("labeler", "TEXT NOT NULL DEFAULT 'llm'"),
        ("themed_at", "TEXT"),
    ),
    "messages": (("parent_channel_id", "TEXT"), ("parent_channel_name", "TEXT")),
    "digests": (("removed_citations", "TEXT NOT NULL DEFAULT '[]'"),),
    "investigations": (("removed_citations", "TEXT NOT NULL DEFAULT '[]'"),),
}


def _migrate(conn: sqlite3.Connection) -> None:
    with conn:
        for table, columns in _ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: LLMClient logs agent_runs from worker threads,
    # serialized by its own lock. All other writes happen on the main thread.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        # Lets the Plan 3 web server read the DB concurrently with the pipeline
        # writing to it.
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn
