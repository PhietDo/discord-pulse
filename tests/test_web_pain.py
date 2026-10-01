from tests.web_fakes import make_client


def test_pain_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/pain")
    assert r.status_code == 200 and "No pain points for this filter yet" in r.text


def test_pain_lists_themes_and_selects_top_one(tmp_path):
    html = make_client(tmp_path).get("/pain").text
    assert "Install fails on M1" in html and "Auth docs" in html
    assert 'id="msg-q1"' in html and 'id="msg-q3"' in html and 'id="msg-q2"' not in html
    assert 'name="status"' in html and "Not triaged" in html


def test_pain_selects_requested_theme(tmp_path):
    html = make_client(tmp_path).get("/pain?theme=2").text
    assert 'id="msg-q2"' in html and 'id="msg-q1"' not in html


def test_pain_unknown_theme_falls_back(tmp_path):
    client = make_client(tmp_path)
    for value in ("999", "abc", "-1"):
        r = client.get(f"/pain?theme={value}")
        assert r.status_code == 200 and 'id="msg-q1"' in r.text


def test_set_status_redirects_and_shows_it(tmp_path):
    client = make_client(tmp_path)
    r = client.post("/pain/1/status?days=14", data={"status": "in_progress", "note": "wheels building"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/pain?days=14&theme=1&saved=1"
    html = client.get(r.headers["location"]).text
    assert "Fix in progress" in html and "wheels building" in html and "Saved." in html


def test_set_status_rejects_bad_input(tmp_path):
    client = make_client(tmp_path)
    assert client.post("/pain/1/status", data={"status": "done", "note": ""}).status_code == 400
    assert client.post("/pain/999/status", data={"status": "acknowledged", "note": ""}).status_code == 404


def test_shipped_theme_shows_before_and_after(tmp_path):
    client = make_client(tmp_path)
    client.post("/pain/1/status", data={"status": "shipped", "note": "2.0.2"})
    html = client.get("/pain?theme=1").text
    assert "Fix shipped" in html and "Before" in html and "After" in html


def test_merged_theme_resolves_to_root(tmp_path):
    client = make_client(tmp_path)
    from pulse.db import connect
    conn = connect(tmp_path / "pulse.db")
    with conn:
        conn.execute("UPDATE themes SET status = 'merged', merged_into = 1 WHERE id = 2")
    conn.close()
    html = client.get("/pain?theme=2").text
    assert 'id="msg-q2"' in html and 'id="msg-q1"' in html  # root theme 1 now includes q2
