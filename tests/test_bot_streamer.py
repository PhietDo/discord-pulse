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


def test_integrity_error_keeps_the_batch(monkeypatch):
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    real = bot_source.upsert_messages
    calls = []

    def integrity_error_once(*args):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.IntegrityError("constraint failed")
        return real(*args)

    monkeypatch.setattr(bot_source, "upsert_messages", integrity_error_once)
    s.add(live(1))
    with pytest.raises(sqlite3.IntegrityError):
        s.flush()
    monkeypatch.undo()
    assert s.flush() == 1
    assert stored(conn) == {"1": "hi"}


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


def test_pitch_doc_discloses_every_data_destination():
    pitch = Path("docs/bot-pitch.md").read_text()
    for phrase in ("deny Send Messages and Add Reactions", "@everyone", "Jev (through OpenRouter)",
                   "GitHub or Linear", "never author names", "Slack alerts", "display name"):
        assert phrase in pitch
    assert "It has no permission to do any of that." not in pitch


def _bot_cli(tmp_path, monkeypatch):
    from tests.test_cli import CONFIG

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "tok")
    monkeypatch.setattr("pulse.run._discord_installed", lambda: True)
    (tmp_path / "pulse.toml").write_text(CONFIG)
    return str(tmp_path / "pulse.toml")


@pytest.mark.parametrize("error, code", [
    ("Discord rejected DISCORD_BOT_TOKEN", 1),
    ("turn on Message Content Intent on the bot's page in the Discord Developer Portal", 1),
    ("the bot is not in server 900; see python -m pulse.run bot-invite", 1),
    ("#help: Forbidden: 403 Missing Access", 0),
])
def test_ingest_from_bot_exits_1_on_fatal_errors_only(tmp_path, monkeypatch, capsys, error, code):
    cfg = _bot_cli(tmp_path, monkeypatch)

    def runner(token, config, since, default_since, errors):
        errors.append(error)
        return []

    monkeypatch.setattr(bot_source, "_default_runner", runner)
    assert main(["--config", cfg, "ingest", "--source", "bot"]) == code
    assert error in capsys.readouterr().out


@pytest.mark.parametrize("fatal, code", [(True, 1), (False, 0)])
def test_bot_command_exits_1_when_the_streamer_hit_a_fatal_error(tmp_path, monkeypatch, fatal, code):
    cfg = _bot_cli(tmp_path, monkeypatch)

    def fake_run_streamer(token, config, conn, **kwargs):
        s = BotStreamer(conn, config)
        s.fatal = fatal
        return s

    monkeypatch.setattr("pulse.sources.bot_client.run_streamer", fake_run_streamer)
    assert main(["--config", cfg, "bot"]) == code


def test_is_fatal_recognises_only_fatal_bot_errors():
    assert bot_source.is_fatal("Discord rejected DISCORD_BOT_TOKEN")
    assert bot_source.is_fatal("the bot is not in server 1; see python -m pulse.run bot-invite")
    assert not bot_source.is_fatal("#general: HTTPException: 500")


def test_readme_states_edit_coverage_and_env_quoting():
    readme = Path("README.md").read_text()
    assert "discord.py keeps about the last 1,000" in readme
    assert "older edits are caught on the next file import" in readme
    assert "Write values in single quotes if they contain spaces or $ (export NAME='value')." in readme
