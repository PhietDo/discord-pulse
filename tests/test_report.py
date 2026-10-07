from pathlib import Path

from pulse.db import connect
from pulse.report import build_report
from pulse.run import main
from tests.test_cli import CONFIG
from tests.web_fakes import CONFIG as WEB_CONFIG, NOW, seed


def seeded(tmp_path):
    conn = connect(tmp_path / "pulse.db")
    seed(conn)
    with conn:
        conn.execute("INSERT INTO reactions (message_id, emoji, count) VALUES ('p1', '🎉', 6)")
    return conn


def test_report_is_self_contained(tmp_path):
    html = build_report(seeded(tmp_path), WEB_CONFIG, NOW, server_name="Acme")
    assert html.startswith("<!doctype html>") and "<style>" in html
    assert "<script" not in html and "http-equiv" not in html
    assert 'src="http' not in html and 'href="http' not in html.replace('href="https://discord.com/channels', "")
    for heading in ("Top pain points", "Coverage gaps", "Newcomers", "Community helpers", "Latest digest"):
        assert heading in html
    assert "Install fails on M1" in html and "Acme" in html


def test_report_anonymizes_by_default(tmp_path):
    conn = seeded(tmp_path)
    anon = build_report(conn, WEB_CONFIG, NOW)
    for name in ("@alice", "@uma", ">alice<", ">uma<", "carol"):
        assert name not in anon
    assert "<script" not in anon and 'class="av"' not in anon
    assert "@a member" in anon
    named = build_report(conn, WEB_CONFIG, NOW, with_names=True)
    assert "@uma" in named and "@alice" in named


def test_report_on_empty_db(tmp_path):
    html = build_report(connect(tmp_path / "pulse.db"), WEB_CONFIG, NOW)
    assert "No pain points" in html and "No newcomers" in html


def test_report_cli_writes_file(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "pulse.toml").write_text(CONFIG)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "r.html"
    assert main(["--config", "pulse.toml", "report", "--days", "14", "--out", str(out), "--name", "Acme"]) == 0
    assert out.read_text().startswith("<!doctype html>") and str(out) in capsys.readouterr().out
    assert main(["--config", "pulse.toml", "report"]) == 0
    assert list(Path("reports").glob("pulse-*.html"))
