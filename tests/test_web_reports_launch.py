import json

from pulse.agents.base import BackendResult
from pulse.db import connect
from pulse.models import to_iso
from tests.fakes import make_config
from tests.web_fakes import CONFIG, NOW, fake_llm_factory, make_client


def cite_first_and_bogus(user):
    first = json.loads(user)["messages"][0]["message_id"]
    return BackendResult({"markdown": f"## What's landing well\nSee [[msg:{first}]] and [[msg:bogus]]."}, 500, 200)


def test_reports_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/reports")
    assert r.status_code == 200 and "No reports yet" in r.text
    assert "Agents are off" in r.text and "disabled" in r.text


def test_reports_list_and_digest_detail(tmp_path):
    client = make_client(tmp_path)
    html = client.get("/reports").text
    assert "Weekly digest" in html and "/reports/digest/1" in html
    detail = client.get("/reports/digest/1").text
    assert "@uma" in detail and 'class="cite"' in detail
    assert 'id="msg-p1"' in detail and 'id="msg-q1"' in detail  # cited messages as cards
    assert client.get("/reports/digest/99").status_code == 404


def test_write_weekly_digest_in_background(tmp_path):
    factory = fake_llm_factory(cite_first_and_bogus)
    client = make_client(tmp_path, llm_factory=factory)
    r = client.post("/reports/digest?days=14", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/reports?days=14&pending={int(NOW.timestamp())}"
    page = client.get(r.headers["location"]).text
    assert "Digest ready" in page and "hx-trigger" not in page
    conn = connect(tmp_path / "pulse.db")
    newest = conn.execute("SELECT id, removed_citations FROM digests ORDER BY id DESC LIMIT 1").fetchone()
    assert json.loads(newest["removed_citations"]) == ["bogus"]
    assert "1 citation removed" in client.get(f"/reports/digest/{newest['id']}").text


def test_pending_digest_polls_until_done(tmp_path):
    client = make_client(tmp_path, llm_factory=fake_llm_factory(cite_first_and_bogus))
    since = int(NOW.timestamp()) + 60  # nothing written after this yet
    page = client.get(f"/reports?pending={since}").text
    assert 'hx-trigger="every 5s"' in page and "Writing the digest" in page


def test_agent_buttons_refused_when_agents_off(tmp_path):
    client = make_client(tmp_path, demo=True, llm_factory=fake_llm_factory(cite_first_and_bogus))
    assert client.post("/reports/digest").status_code == 409
    assert client.post("/launch/1/digest").status_code == 409
    assert "Agents are off in demo mode" in client.get("/reports").text


def test_investigation_detail_running_and_done(tmp_path):
    client = make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    with conn:
        conn.execute("INSERT INTO investigations (id, question, context, created_at) VALUES (1, 'why?', '{}', ?)", (to_iso(NOW),))
        conn.execute(
            "INSERT INTO investigations (id, question, context, markdown, cited_message_ids, created_at)"
            " VALUES (2, 'why else?', '{}', 'Because [[msg:q1]].', '[\"q1\"]', ?)", (to_iso(NOW),))
    running = client.get("/reports/investigation/1").text
    assert 'hx-trigger="every 3s"' in running and "Investigating" in running
    done = client.get("/reports/investigation/2").text
    assert "hx-trigger" not in done and "@alice" in done and 'id="msg-q1"' in done
    assert client.get("/reports/investigation/9").status_code == 404


def test_launch_empty(tmp_path):
    r = make_client(tmp_path, seeded=False, config=make_config()).get("/launch")
    assert r.status_code == 200 and "No launches yet" in r.text


def test_launch_before_after(tmp_path):
    html = make_client(tmp_path).get("/launch").text
    assert "v2.0" in html and "<svg class=\"chart\"" in html
    assert "6 messages" in html and "0 messages" in html  # after vs before
    assert 'id="msg-q1"' in html  # keyword "install"


def test_launch_in_the_future(tmp_path):
    from pulse.config import Launch

    cfg = make_config(launches=(Launch("v3.0", "2026-12-01", ("v3",)),))
    client = make_client(tmp_path, config=cfg)
    conn = connect(tmp_path / "pulse.db")
    from pulse.store import sync_launches
    sync_launches(conn, cfg.launches)
    conn.close()
    html = client.get("/launch?name=v3.0").text
    assert "hasn't happened yet" in html


def test_launch_digest_in_background(tmp_path):
    client = make_client(tmp_path, llm_factory=fake_llm_factory(cite_first_and_bogus))
    r = client.post("/launch/1/digest", follow_redirects=False)
    assert r.status_code == 303 and "pending=" in r.headers["location"]
    conn = connect(tmp_path / "pulse.db")
    assert conn.execute("SELECT kind FROM digests ORDER BY id DESC LIMIT 1").fetchone()["kind"] == "launch"
    assert client.post("/launch/99/digest").status_code == 404


def test_digest_over_the_cap_shows_failed_banner(tmp_path):
    from dataclasses import replace

    from pulse.agents.llm import LLMClient
    from tests.fakes import FakeBackend

    backend = FakeBackend(handler=cite_first_and_bogus)

    def factory(conn, config):  # the LLMClient's clock matches the dashboard's
        return LLMClient(conn, config, {"anthropic": backend}, now=lambda: NOW, sleep=lambda s: None)

    client = make_client(tmp_path, llm_factory=factory, config=replace(CONFIG, daily_usd_cap=0.0))
    r = client.post("/reports/digest", follow_redirects=False)
    assert r.status_code == 303 and f"pending={int(NOW.timestamp())}" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "The digest failed: the daily budget cap was reached" in page
    assert backend.calls == []  # refused before any model call
