from pulse.db import connect
from tests.web_fakes import make_client


def test_bugs_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/bugs")
    assert r.status_code == 200 and "No bug reports in this window" in r.text


def test_bugs_grouped_by_pain_point_with_reply_state(tmp_path):
    html = make_client(tmp_path).get("/bugs").text
    install = html.index("Install fails on M1")
    ungrouped = html.index("Not yet grouped")
    assert install < ungrouped
    assert "2 reports" in html and "2 without staff reply" in html
    assert 'id="msg-q1"' in html and 'id="msg-q3"' in html and 'id="msg-g2"' in html
    assert 'id="msg-q2"' not in html  # docs, not a bug


def test_bugs_channel_filter(tmp_path):
    html = make_client(tmp_path).get("/bugs?channel=200").text
    assert 'id="msg-g2"' in html and 'id="msg-q1"' not in html


def test_queue_lists_open_items_in_priority_order(tmp_path):
    html = make_client(tmp_path).get("/queue").text
    assert html.index('id="msg-q1"') < html.index('id="msg-g2"') < html.index('id="msg-q3"')
    assert "Mark handled" in html and "Dismiss" in html


def test_queue_reason_filter(tmp_path):
    html = make_client(tmp_path).get("/queue?reason=unanswered").text
    assert 'id="msg-q3"' in html and 'id="msg-q1"' not in html


def test_queue_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/queue")
    assert r.status_code == 200 and "Nothing is waiting for a reply" in r.text


def _qid(tmp_path, message_id):
    conn = connect(tmp_path / "pulse.db")
    qid = conn.execute("SELECT id FROM mod_queue WHERE message_id = ?", (message_id,)).fetchone()[0]
    conn.close()
    return qid


def test_close_from_form_redirects_and_removes_item(tmp_path):
    client = make_client(tmp_path)
    qid = _qid(tmp_path, "q1")
    r = client.post(f"/queue/{qid}/close?days=14", data={"action": "handled"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/queue?days=14"
    assert 'id="msg-q1"' not in client.get("/queue").text


def test_close_from_htmx_returns_fragment(tmp_path):
    client = make_client(tmp_path)
    r = client.post(f"/queue/{_qid(tmp_path, 'g2')}/close", data={"action": "dismissed"}, headers={"HX-Request": "true"})
    assert r.status_code == 200 and "Dismissed" in r.text and 'id="msg-g2"' in r.text


def test_close_already_closed_item_is_404(tmp_path):
    client = make_client(tmp_path)
    qid = _qid(tmp_path, "q3")
    assert client.post(f"/queue/{qid}/close", data={"action": "handled"}).status_code in (200, 303)
    assert client.post(f"/queue/{qid}/close", data={"action": "handled"}).status_code == 404
    assert client.post("/queue/99999/close", data={"action": "handled"}).status_code == 404
    assert client.post(f"/queue/{qid}/close", data={"action": "reopen"}).status_code == 400


def test_bug_reply_after_now_does_not_count(tmp_path):
    from dataclasses import replace
    from datetime import timedelta

    from pulse.store import upsert_messages
    from pulse.web.filters import parse_filters
    from pulse.web.queries import bug_groups
    from tests.fakes import T0, msg
    from tests.web_fakes import NOW

    make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    later = (NOW - T0) + timedelta(hours=1)  # an hour after the dashboard's "now"
    reply = replace(msg("s9", "fixed now", minutes=later.total_seconds() / 60, channel_id="200",
                        author_id="t1", author_name="sam", reply_to_id="g2"), channel_name="general")
    upsert_messages(conn, [reply], frozenset({"t1"}))
    groups = bug_groups(conn, parse_filters(None, None, NOW, set()))
    assert any("g2" in g["unanswered_ids"] for g in groups)


def test_failed_digest_job_logs_traceback(tmp_path, caplog):
    import logging

    from pulse.agents.base import LLMError
    from pulse.web import jobs

    def broken(conn, config):
        raise LLMError("provider down")

    client = make_client(tmp_path, llm_factory=broken)
    with caplog.at_level(logging.WARNING, logger="pulse.web.jobs"):
        jobs.run_digest_job(client.app.state.settings, None)
    record = next(r for r in caplog.records if "digest job failed" in r.getMessage())
    assert record.exc_info is not None
