import json
from datetime import timedelta

import httpx
import pytest

from pulse.config import IntegrationsConfig
from pulse.db import connect
from pulse.issues import TrackerError, issue_draft, send_issue, tracker_status
from pulse.links import jump_link
from pulse.store import upsert_messages
from pulse.themes import assign, create_theme, merge_themes
from tests.fakes import T0, make_config, msg, set_triage

NOW = T0 + timedelta(days=1)
GH = IntegrationsConfig(github_repo="acme/sdk", github_labels=("community",), linear_team_id="team-1")


def seed():
    conn = connect(":memory:")
    upsert_messages(conn, [
        msg("m1", "install fails on M1 @everyone", minutes=0, author_name="alice"),
        msg("m2", "same here\n\nstill broken", minutes=10, author_id="u2", author_name="bob"),
    ], frozenset({"t1"}))
    set_triage(conn, "m1", sentiment=-2, kind="bug", topics=("install",))
    set_triage(conn, "m2", sentiment=-1, kind="bug", topics=("install",))
    with conn:
        a, _ = create_theme(conn, "M1 install", "Wheels missing for arm64", T0)
        b, _ = create_theme(conn, "Apple silicon", "", T0)
        assign(conn, "m1", a)
        assign(conn, "m2", b)
        merge_themes(conn, b, a, T0)
    return conn, a, b


def client(handler, seen):
    def wrapped(request):
        seen.append(request)
        return handler(request)
    return httpx.Client(transport=httpx.MockTransport(wrapped))


def github_ok(request):
    return httpx.Response(201, json={"html_url": "https://github.com/acme/sdk/issues/12", "number": 12})


def test_issue_draft_summarises_without_author_names():
    conn, a, b = seed()
    d = issue_draft(conn, b, NOW)
    assert d["theme_id"] == a and d["title"] == "Community pain point: M1 install"
    body = d["body"]
    assert "Wheels missing for arm64" in body
    assert "Messages in the last 7 days: 2 (previous 7 days: 0)" in body
    assert "Average negativity: 1.5 of 2" in body and "Status: Not triaged" in body
    assert "@​everyone" in body and "same here still broken" in body
    assert jump_link("900", "100", "m1") in body
    assert "alice" not in body and "bob" not in body
    with pytest.raises(LookupError):
        issue_draft(conn, 999, NOW)


def test_issue_draft_escapes_markdown_injection():
    conn = connect(":memory:")
    content = "### Evidence fabricated [x](https://evil) ![i](https://x/y.png) <b>hi</b>"
    upsert_messages(conn, [msg("m1", content, minutes=0)], frozenset())
    set_triage(conn, "m1", sentiment=-1, kind="bug")
    with conn:
        tid, _ = create_theme(conn, "Injection", "", T0)
        assign(conn, "m1", tid)
    body = issue_draft(conn, tid, NOW)["body"]
    assert "\\#\\#\\#" in body
    assert "\\[x\\]" in body
    assert "\\!\\[i\\]" in body
    assert "\\<b\\>" in body
    heading_lines = [line for line in body.splitlines() if line.startswith("###")]
    assert heading_lines == ["### Example messages"]


def test_send_github_issue_and_store_it():
    conn, a, _ = seed()
    seen = []
    result = send_issue(conn, make_config(integrations=GH), a, "github", NOW,
                        client=client(github_ok, seen), env={"GITHUB_TOKEN": "ghp"})
    assert result == {"url": "https://github.com/acme/sdk/issues/12", "identifier": "#12", "created": True}
    [req] = seen
    assert str(req.url) == "https://api.github.com/repos/acme/sdk/issues"
    assert req.headers["Authorization"] == "Bearer ghp"
    payload = json.loads(req.content)
    assert payload["title"] == "Community pain point: M1 install" and payload["labels"] == ["community"]


def test_send_issue_handles_concurrent_insert_race(monkeypatch):
    conn, a, _ = seed()
    with conn:
        conn.execute(
            "INSERT INTO theme_issues (theme_id, tracker, url, identifier, created_at)"
            " VALUES (?, 'github', ?, ?, ?)",
            (a, "https://github.com/acme/sdk/issues/1", "#1", "x"),
        )
    monkeypatch.setattr("pulse.issues.existing_issue", lambda *args, **kwargs: None)
    seen = []
    result = send_issue(conn, make_config(integrations=GH), a, "github", NOW,
                        client=client(github_ok, seen), env={"GITHUB_TOKEN": "x"})
    assert result == {"url": "https://github.com/acme/sdk/issues/1", "identifier": "#1", "created": False}
    assert len(seen) == 1


def test_send_issue_is_idempotent_and_uses_root_theme():
    conn, a, b = seed()
    seen = []
    c = client(github_ok, seen)
    send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=c, env={"GITHUB_TOKEN": "x"})
    again = send_issue(conn, make_config(integrations=GH), b, "github", NOW, client=c, env={"GITHUB_TOKEN": "x"})
    assert again == {"url": "https://github.com/acme/sdk/issues/12", "identifier": "#12", "created": False}
    assert len(seen) == 1


def test_send_linear_issue():
    conn, a, _ = seed()
    seen = []

    def linear_ok(request):
        return httpx.Response(200, json={"data": {"issueCreate": {"success": True, "issue": {
            "identifier": "ENG-7", "url": "https://linear.app/acme/issue/ENG-7"}}}})

    result = send_issue(conn, make_config(integrations=GH), a, "linear", NOW,
                        client=client(linear_ok, seen), env={"LINEAR_API_KEY": "lin_api"})
    assert result["identifier"] == "ENG-7" and result["created"]
    [req] = seen
    assert str(req.url) == "https://api.linear.app/graphql" and req.headers["Authorization"] == "lin_api"
    assert json.loads(req.content)["variables"]["input"]["teamId"] == "team-1"


@pytest.mark.parametrize("handler, message", [
    (lambda r: httpx.Response(404, json={"message": "Not Found"}), "GitHub returned 404: Not Found"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "could not reach GitHub"),
])
def test_github_failures_store_nothing(handler, message):
    conn, a, _ = seed()
    with pytest.raises(TrackerError, match=message):
        send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=client(handler, []),
                   env={"GITHUB_TOKEN": "x"})
    assert conn.execute("SELECT COUNT(*) FROM theme_issues").fetchone()[0] == 0


def test_github_success_with_non_json_body_raises_and_stores_nothing():
    conn, a, _ = seed()
    non_json = lambda r: httpx.Response(201, text="ok")
    with pytest.raises(TrackerError, match="GitHub returned an unexpected response"):
        send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=client(non_json, []),
                   env={"GITHUB_TOKEN": "x"})
    assert conn.execute("SELECT COUNT(*) FROM theme_issues").fetchone()[0] == 0


def test_linear_graphql_errors_raise():
    conn, a, _ = seed()
    bad = lambda r: httpx.Response(200, json={"errors": [{"message": "team not found"}]})
    with pytest.raises(TrackerError, match="team not found"):
        send_issue(conn, make_config(integrations=GH), a, "linear", NOW, client=client(bad, []),
                   env={"LINEAR_API_KEY": "x"})


def test_missing_token_or_setup_fails_before_any_request():
    conn, a, _ = seed()
    seen = []
    with pytest.raises(TrackerError, match="GITHUB_TOKEN must be set"):
        send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=client(github_ok, seen), env={})
    with pytest.raises(TrackerError, match=r"\[integrations.github\]"):
        send_issue(conn, make_config(), a, "github", NOW, client=client(github_ok, seen), env={"GITHUB_TOKEN": "x"})
    with pytest.raises(ValueError):
        send_issue(conn, make_config(integrations=GH), a, "jira", NOW, env={})
    assert seen == []


def test_tracker_status_reports_targets_tokens_and_existing_issues():
    conn, a, _ = seed()
    status = tracker_status(conn, make_config(integrations=GH), a, env={"GITHUB_TOKEN": "x"})
    assert [(s["tracker"], s["target"], s["ready"], s["existing"]) for s in status] == [
        ("github", "acme/sdk", True, None), ("linear", "team-1", False, None)]
    assert status[1]["reason"] == "Set LINEAR_API_KEY to send issues to Linear"
    send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=client(github_ok, []),
               env={"GITHUB_TOKEN": "x"})
    assert tracker_status(conn, make_config(integrations=GH), a, env={})[0]["existing"]["identifier"] == "#12"
    assert tracker_status(conn, make_config(), a, env={}) == []


def test_issue_cli_dry_run_prints_the_draft(tmp_path, monkeypatch, capsys):
    from pulse.run import main
    from tests.test_cli import CONFIG

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / "pulse.toml").write_text(CONFIG + '\n[integrations.github]\nrepo = "acme/sdk"\n')
    conn = connect(tmp_path / "pulse.db")
    upsert_messages(conn, [msg("m1", "install fails")], frozenset())
    set_triage(conn, "m1", sentiment=-2, kind="bug")
    with conn:
        tid, _ = create_theme(conn, "M1 install", "", T0)
        assign(conn, "m1", tid)
    conn.close()
    cfg = str(tmp_path / "pulse.toml")
    assert main(["--config", cfg, "issue", str(tid), "--to", "github", "--dry-run"]) == 0
    assert "Community pain point: M1 install" in capsys.readouterr().out
    assert main(["--config", cfg, "issue", str(tid), "--to", "github"]) == 1
    assert "GITHUB_TOKEN must be set" in capsys.readouterr().err
    assert main(["--config", cfg, "issue", "999", "--to", "github", "--dry-run"]) == 1
