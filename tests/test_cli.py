import shutil
from pathlib import Path

import pytest

from pulse.db import connect
from pulse.run import main

FIXTURES = Path(__file__).parent / "fixtures"

CONFIG = '''
[server]
guild_id = "900"
team_member_ids = ["t1"]

[models]
triage = "anthropic:m"
theme = "anthropic:m"
digest = "anthropic:m"
investigate = "anthropic:m"

[pricing."anthropic:m"]
input = 1.0
output = 5.0
'''


def test_config_error_exits_2(tmp_path, capsys):
    (tmp_path / "pulse.toml").write_text("[server]\n")
    assert main(["--config", str(tmp_path / "pulse.toml"), "ingest"]) == 2
    assert "guild_id" in capsys.readouterr().err


def test_ingest_command_imports_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    (tmp_path / "imports").mkdir()
    shutil.copy(FIXTURES / "dce_channel.json", tmp_path / "imports" / "dce_channel.json")

    assert main(["--config", str(tmp_path / "pulse.toml"), "ingest"]) == 0

    out = capsys.readouterr().out
    assert "inserted 3" in out
    conn = connect(tmp_path / "pulse.db")
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
    assert conn.execute("SELECT is_team FROM messages WHERE id = '1002'").fetchone()[0] == 1


def test_triage_force_without_since_exits_with_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    with pytest.raises(SystemExit) as exc_info:
        main(["--config", str(tmp_path / "pulse.toml"), "triage", "--force"])
    assert exc_info.value.code == 2


from datetime import datetime, timedelta, timezone

from pulse.models import Message
from pulse.store import upsert_messages


def test_queue_command_lists_open_items(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    conn = connect(tmp_path / "pulse.db")
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    upsert_messages(conn, [Message(id="f1", guild_id="900", channel_id="100", author_id="u1",
                                   author_name="erin", content="prod is down after the upgrade",
                                   created_at=recent, channel_name="help")], frozenset())
    with conn:
        conn.execute(
            "INSERT INTO triage (message_id, sentiment, confidence, kind, topics, needs_reply, prompt_version,"
            " created_at) VALUES ('f1', -2, 0.9, 'bug', '[]', 1, 'test', 'x')"
        )
    conn.close()

    assert main(["--config", str(tmp_path / "pulse.toml"), "modqueue"]) == 0
    assert main(["--config", str(tmp_path / "pulse.toml"), "queue", "--limit", "5"]) == 0

    out = capsys.readouterr().out
    assert "[frustrated" in out
    assert "erin in #help" in out
    assert "https://discord.com/channels/900/100/f1" in out
