import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from pulse.db import connect
from pulse.run import main
from pulse.sources import bot_source
from pulse.sources.bot_source import BotStreamer
from tests.fakes import make_config

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
HELP = NS(id=100, name="help")
GENERAL = NS(id=200, name="general")


def live(id, content="hi", *, channel=HELP, guild_id=900, kind="default", minutes=0):
    return NS(
        id=id, type=NS(name=kind), channel=channel, guild=NS(id=guild_id) if guild_id else None,
        content=content, created_at=T0 + timedelta(minutes=minutes), edited_at=None, reference=None,
        author=NS(id=1, name="alice", display_name="Alice", bot=False, display_avatar=None),
    )


def stored(conn):
    return {r["id"]: r["content"] for r in conn.execute("SELECT id, content FROM messages")}


def test_add_keeps_only_wanted_messages_from_this_server():
    s = BotStreamer(connect(":memory:"), make_config(channel_ids=("100",)))
    assert s.add(live(1))
    assert not s.add(live(2, guild_id=999))
    assert not s.add(live(3, guild_id=None))
    assert not s.add(live(4, kind="pins_add"))
    assert not s.add(live(5, channel=GENERAL))
    assert s.received == 1


def test_flush_writes_latest_version_once():
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    s.add(live(1, "first"))
    s.add(live(1, "edited"))
    s.add(live(2, "second"))
    assert s.flush() == 2 and s.written == 2
    assert stored(conn) == {"1": "edited", "2": "second"}
    assert s.flush() == 0


def test_flush_keeps_messages_when_database_is_locked(monkeypatch):
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    real = bot_source.upsert_messages
    calls = []

    def locked_once(*args):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args)

    monkeypatch.setattr(bot_source, "upsert_messages", locked_once)
    s.add(live(1, "first"))
    assert s.flush() == 0 and s.flush_failures == 1 and stored(conn) == {}
    s.add(live(1, "edited while locked"))
    assert s.flush() == 1
    assert stored(conn) == {"1": "edited while locked"}


def test_other_database_errors_propagate_but_keep_the_batch(monkeypatch):
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    monkeypatch.setattr(bot_source, "upsert_messages",
                        lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    s.add(live(1))
    with pytest.raises(sqlite3.OperationalError):
        s.flush()
    monkeypatch.undo()
    assert s.flush() == 1


def test_add_many_and_since():
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    s.add_many([bot_source.message_from_discord(live(7, minutes=3), "900")])
    s.flush()
    assert s.since() == {"100": T0 + timedelta(minutes=3)}


def test_bot_invite_prints_the_read_only_link(capsys):
    assert main(["bot-invite", "--client-id", "123456"]) == 0
    out = capsys.readouterr().out
    assert "client_id=123456&scope=bot&permissions=66560" in out
    with pytest.raises(SystemExit):
        main(["bot-invite", "--client-id", "abc"])


def test_bot_command_without_token_exits_2(tmp_path, monkeypatch, capsys):
    from tests.test_cli import CONFIG

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr("pulse.run._discord_installed", lambda: True)
    (tmp_path / "pulse.toml").write_text(CONFIG)
    assert main(["--config", str(tmp_path / "pulse.toml"), "bot"]) == 2
    assert "DISCORD_BOT_TOKEN must be set" in capsys.readouterr().err


def test_pitch_doc_states_the_exact_access():
    pitch = Path("docs/bot-pitch.md").read_text()
    for phrase in ("View Channels", "Read Message History", "Message Content Intent", "never posts",
                   "bot-invite", "How to remove it"):
        assert phrase in pitch
    readme = Path("README.md").read_text()
    assert "ingest --source bot" in readme and "docs/bot-pitch.md" in readme
