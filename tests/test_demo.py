import pytest
from fastapi.testclient import TestClient

from pulse.db import connect
from pulse.demo import demo_config, is_demo_db, seed_demo
from pulse.web.app import create_app
from pulse.web.settings import WebSettings
from tests.web_fakes import NOW


def test_seed_demo_counts(tmp_path):
    counts = seed_demo(tmp_path / "demo.db", NOW)
    assert counts["messages"] > 400
    assert counts["themes"] == 8
    assert counts["open_queue"] > 0
    assert counts["digests"] == 1 and counts["investigations"] == 1
    assert counts["runs"] > 20
    assert is_demo_db(tmp_path / "demo.db")


def test_seed_demo_is_deterministic(tmp_path):
    a = seed_demo(tmp_path / "a.db", NOW)
    b = seed_demo(tmp_path / "b.db", NOW)
    assert a == b
    first = lambda p: connect(p).execute("SELECT content FROM messages ORDER BY created_at, id LIMIT 1").fetchone()[0]
    assert first(tmp_path / "a.db") == first(tmp_path / "b.db")


def test_seed_demo_refuses_to_overwrite_real_db_but_replaces_demo(tmp_path):
    real = tmp_path / "pulse.db"
    connect(real).close()
    with pytest.raises(FileExistsError):
        seed_demo(real, NOW)
    demo = tmp_path / "demo.db"
    seed_demo(demo, NOW)
    assert seed_demo(demo, NOW)["themes"] == 8  # a previous demo is replaced


def test_demo_dashboard_pages_render(tmp_path):
    db = tmp_path / "demo.db"
    seed_demo(db, NOW)
    app = create_app(WebSettings(db_path=db, config=demo_config(db, NOW), server_name="Acme SDK Community (demo)",
                                 demo=True, clock=lambda: NOW))
    client = TestClient(app)
    for path in ("/", "/pain", "/bugs", "/queue", "/messages", "/launch", "/reports", "/runs",
                 "/reports/digest/1", "/reports/investigation/1", "/?days=90", "/pain?days=7"):
        assert client.get(path).status_code == 200, path
    pain = client.get("/pain").text
    assert "Fix shipped" in pain and "Fix in progress" in pain and "Acknowledged" in pain
    assert "Agents are off in demo mode" in client.get("/reports").text
    assert "M1 install fails on v2" in client.get("/messages?q=wheel").text


def test_demo_has_community_data(tmp_path):
    from datetime import datetime, timedelta, timezone

    from pulse import community
    from pulse.db import connect
    from pulse.demo import seed_demo

    now = datetime(2026, 10, 7, 12, tzinfo=timezone.utc)
    seed_demo(tmp_path / "demo.db", now)
    conn = connect(tmp_path / "demo.db")
    start = now - timedelta(days=30)
    nc = community.newcomers(conn, start, now)
    assert nc["count"] >= 8 and 0 < nc["replied"] < nc["count"] and nc["returned"] > 0
    board = community.helpers(conn, start, now)
    assert len(board) >= 2 and all(not h["author_id"].startswith("t") for h in board)
    assert community.most_wanted(conn, start, now) and community.top_reacted(conn, start, now)
