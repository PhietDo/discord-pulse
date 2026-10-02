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

LAUNCH_CONFIG = CONFIG + '''
[[launches]]
name = "v2.0 SDK"
date = "2026-09-15"
keywords = ["v2"]
'''


def test_commands_sync_launches_and_themes_runs_with_nothing_to_do(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(LAUNCH_CONFIG)
    assert main(["--config", str(tmp_path / "pulse.toml"), "themes"]) == 0
    assert "themes: considered 0" in capsys.readouterr().out
    assert connect(tmp_path / "pulse.db").execute("SELECT name FROM launches").fetchone()[0] == "v2.0 SDK"


def test_digest_unknown_launch_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(LAUNCH_CONFIG)
    assert main(["--config", str(tmp_path / "pulse.toml"), "digest", "--launch", "nope"]) == 1
    assert "unknown launch 'nope'" in capsys.readouterr().err


def test_investigate_unknown_theme_exits_1_before_any_model_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    monkeypatch.setattr("pulse.run.build_llm", lambda *a: pytest.fail("no model client should be built"))
    assert main(["--config", str(tmp_path / "pulse.toml"), "investigate", "why?", "--theme", "99"]) == 1
    assert "unknown theme 99" in capsys.readouterr().err
    conn = connect(tmp_path / "pulse.db")
    assert conn.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0


def _capture_serve(monkeypatch):
    served = {}
    monkeypatch.setattr("pulse.run.uvicorn.run",
                        lambda app, host, port, **kw: served.update(app=app, host=host, port=port))
    return served


def test_seed_demo_then_web_demo(tmp_path, monkeypatch, capsys):
    db = tmp_path / "demo.db"
    assert main(["seed-demo", "--db", str(db)]) == 0
    assert "web --demo" in capsys.readouterr().out
    served = _capture_serve(monkeypatch)
    assert main(["web", "--demo", "--db", str(db), "--port", "9000"]) == 0
    assert served["port"] == 9000 and served["host"] == "127.0.0.1"
    settings = served["app"].state.settings
    assert settings.demo and not settings.agents_on and settings.db_path == db


def test_web_demo_without_db_exits_2(tmp_path, capsys):
    assert main(["web", "--demo", "--db", str(tmp_path / "missing.db")]) == 2
    assert "seed-demo" in capsys.readouterr().err


def test_seed_demo_refuses_real_db(tmp_path, capsys):
    real = tmp_path / "pulse.db"
    connect(real).close()
    assert main(["seed-demo", "--db", str(real)]) == 2
    assert "refusing to overwrite" in capsys.readouterr().err


def test_web_with_config_turns_agents_on(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    served = _capture_serve(monkeypatch)
    assert main(["--config", str(tmp_path / "pulse.toml"), "web", "--name", "Acme"]) == 0
    settings = served["app"].state.settings
    assert settings.agents_on and not settings.demo and settings.server_name == "Acme"
    assert settings.db_path.name == "pulse.db"


def test_web_on_loopback_only_trusts_local_host_names(tmp_path, monkeypatch, capsys):
    db = tmp_path / "demo.db"
    assert main(["seed-demo", "--db", str(db)]) == 0
    served = _capture_serve(monkeypatch)
    for host in ("127.0.0.1", "localhost", "::1"):
        capsys.readouterr()
        assert main(["web", "--demo", "--db", str(db), "--host", host]) == 0
        assert served["app"].state.settings.allowed_hosts == ("127.0.0.1", "localhost", "[::1]", "::1")
        assert "no login" not in capsys.readouterr().err


def test_web_on_all_interfaces_warns_and_trusts_any_host(tmp_path, monkeypatch, capsys):
    db = tmp_path / "demo.db"
    assert main(["seed-demo", "--db", str(db)]) == 0
    served = _capture_serve(monkeypatch)
    assert main(["web", "--demo", "--db", str(db), "--host", "0.0.0.0"]) == 0
    assert served["app"].state.settings.allowed_hosts is None
    assert "Serving on 0.0.0.0 with no login: anyone on your network can read the dashboard" in capsys.readouterr().err
