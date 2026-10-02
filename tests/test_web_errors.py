import sqlite3

from tests.web_fakes import make_client


def _qid(tmp_path, message_id):
    from pulse.db import connect

    conn = connect(tmp_path / "pulse.db")
    qid = conn.execute("SELECT id FROM mod_queue WHERE message_id = ?", (message_id,)).fetchone()[0]
    conn.close()
    return qid


def test_locked_database_on_write_is_503(tmp_path, monkeypatch):
    client = make_client(tmp_path)

    def locked(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("pulse.web.views.queue.close_item", locked)
    r = client.post(f"/queue/{_qid(tmp_path, 'q1')}/close", data={"action": "handled"})
    assert r.status_code == 503
    assert "The pipeline is writing right now; try again in a few seconds." in r.text


def test_http_errors_render_an_html_page(tmp_path):
    client = make_client(tmp_path)
    r = client.get("/reports/digest/99")
    assert r.status_code == 404 and r.headers["content-type"].startswith("text/html")
    assert "<h1>" in r.text and "No such digest" in r.text
    assert '/static/pulse.css' in r.text and 'href="/"' in r.text
    missing = client.get("/no-such-page")
    assert missing.status_code == 404 and "<h1>" in missing.text


def test_http_errors_for_htmx_are_a_fragment(tmp_path):
    client = make_client(tmp_path)
    qid = _qid(tmp_path, "q3")
    client.post(f"/queue/{qid}/close", data={"action": "handled"})
    r = client.post(f"/queue/{qid}/close", data={"action": "handled"}, headers={"HX-Request": "true"})
    assert r.status_code == 404
    assert "<html" not in r.text and "already closed" in r.text


def test_htmx_is_served_locally_and_swaps_error_responses(tmp_path):
    client = make_client(tmp_path)
    html = client.get("/").text
    assert '<script src="/static/htmx.min.js" defer></script>' in html and "unpkg.com" not in html
    assert '"code":"[45]..","swap":true,"error":true' in html
    js = client.get("/static/htmx.min.js")
    assert js.status_code == 200 and 'version:"2.0.4"' in js.text
