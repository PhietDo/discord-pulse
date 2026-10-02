import plistlib
from pathlib import Path

import pytest

from pulse import schedule
from pulse.run import main
from pulse.schedule import JOBS, ScheduleError, build_plist, install, label, uninstall


def plist_for(name, tmp_path):
    return build_plist(
        JOBS[name], python=Path("/opt/py/bin/python"), config=tmp_path / "my dir" / "pulse.toml",
        env_file=tmp_path / "keys env", log_dir=tmp_path / "logs",
    )


def test_pipeline_plist_runs_every_30_minutes_and_sources_the_env_file(tmp_path):
    p = plist_for("pipeline", tmp_path)
    assert p["Label"] == "com.discordpulse.pipeline"
    assert p["StartInterval"] == 1800 and p["RunAtLoad"] is True
    assert p["WorkingDirectory"] == str(tmp_path / "my dir")
    assert p["StandardOutPath"] == p["StandardErrorPath"] == str(tmp_path / "logs" / "pipeline.log")
    shell, flag, script = p["ProgramArguments"]
    assert (shell, flag) == ("/bin/zsh", "-c")
    assert f"'{tmp_path / 'keys env'}'" in script
    assert script.endswith(f"exec /opt/py/bin/python -m pulse.run --config '{tmp_path / 'my dir' / 'pulse.toml'}' pipeline")
    plistlib.loads(plistlib.dumps(p))


def test_digest_is_weekly_and_bot_is_kept_alive(tmp_path):
    d = plist_for("digest", tmp_path)
    assert d["StartCalendarInterval"] == {"Weekday": 1, "Hour": 9, "Minute": 0} and "StartInterval" not in d
    b = plist_for("bot", tmp_path)
    assert b["KeepAlive"] is True and b["RunAtLoad"] is True and b["ThrottleInterval"] == 60


def test_install_writes_plists_and_bootstraps_without_secrets(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text("export ANTHROPIC_API_KEY=sk-secret-value\n")
    calls = []
    agents = tmp_path / "LaunchAgents"
    paths = install(
        ["pipeline", "digest"], python=Path("/py"), config=tmp_path / "pulse.toml", env_file=env_file,
        log_dir=tmp_path / "logs", agents_dir=agents, runner=lambda cmd: calls.append(cmd) or 0, uid=501,
    )
    assert paths == [agents / "com.discordpulse.pipeline.plist", agents / "com.discordpulse.digest.plist"]
    assert calls == [
        ["launchctl", "bootout", "gui/501/com.discordpulse.pipeline"],
        ["launchctl", "bootstrap", "gui/501", str(paths[0])],
        ["launchctl", "bootout", "gui/501/com.discordpulse.digest"],
        ["launchctl", "bootstrap", "gui/501", str(paths[1])],
    ]
    for p in paths:
        assert b"sk-secret-value" not in p.read_bytes()
    assert (tmp_path / "logs").is_dir()


def test_failed_bootstrap_raises(tmp_path):
    runner = lambda cmd: 5 if cmd[1] == "bootstrap" else 0
    with pytest.raises(ScheduleError, match="exit 5"):
        install(["pipeline"], python=Path("/py"), config=tmp_path / "pulse.toml", env_file=tmp_path / "env",
                log_dir=tmp_path / "logs", agents_dir=tmp_path / "A", runner=runner, uid=501)


def test_uninstall_boots_out_and_removes(tmp_path):
    agents = tmp_path / "A"
    agents.mkdir()
    (agents / f"{label('pipeline')}.plist").write_text("x")
    calls = []
    removed = uninstall(["pipeline", "digest", "bot"], agents_dir=agents,
                        runner=lambda cmd: calls.append(cmd) or 0, uid=501)
    assert removed == [agents / "com.discordpulse.pipeline.plist"]
    assert len(calls) == 3 and all(c[1] == "bootout" for c in calls)


def test_schedule_cli_show_install_uninstall(tmp_path, monkeypatch, capsys):
    (tmp_path / "pulse.toml").write_text("[server]\n")  # not loaded: schedule never needs keys
    calls = []
    monkeypatch.setattr(schedule, "AGENTS_DIR", tmp_path / "A")
    monkeypatch.setattr(schedule, "run_launchctl", lambda cmd: calls.append(cmd) or 0)
    cfg = str(tmp_path / "pulse.toml")
    assert main(["--config", cfg, "schedule", "show", "--with-bot"]) == 0
    out = capsys.readouterr().out
    assert out.count("<key>Label</key>") == 3
    assert main(["--config", cfg, "schedule", "install", "--env-file", str(tmp_path / "missing")]) == 0
    captured = capsys.readouterr()
    assert "com.discordpulse.pipeline.plist" in captured.out and "not found" in captured.err
    assert sorted(p.name for p in (tmp_path / "A").iterdir()) == [
        "com.discordpulse.digest.plist", "com.discordpulse.pipeline.plist"]
    assert main(["--config", cfg, "schedule", "uninstall"]) == 0
    assert list((tmp_path / "A").iterdir()) == []
    assert main(["--config", str(tmp_path / "nope.toml"), "schedule", "install"]) == 2
