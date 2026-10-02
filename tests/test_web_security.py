from pulse.agents.base import BackendResult, StepResult
from pulse.db import connect
from tests.fakes import FakeBackend, make_llm
from tests.web_fakes import fake_llm_factory, make_client


def _qid(tmp_path, message_id):
    conn = connect(tmp_path / "pulse.db")
    qid = conn.execute("SELECT id FROM mod_queue WHERE message_id = ?", (message_id,)).fetchone()[0]
    conn.close()
    return qid


def _status(tmp_path, qid):
    conn = connect(tmp_path / "pulse.db")
    status = conn.execute("SELECT status FROM mod_queue WHERE id = ?", (qid,)).fetchone()[0]
    conn.close()
    return status


def test_cross_site_post_is_refused_and_item_stays_open(tmp_path):
    client = make_client(tmp_path)
    qid = _qid(tmp_path, "q1")
    for site in ("cross-site", "same-site"):
        r = client.post(f"/queue/{qid}/close", data={"action": "handled"}, headers={"Sec-Fetch-Site": site})
        assert r.status_code == 403 and r.text == "Cross-site request refused"
    assert _status(tmp_path, qid) == "open"


def test_origin_mismatch_is_refused_and_same_origin_allowed(tmp_path):
    client = make_client(tmp_path)
    qid = _qid(tmp_path, "q1")
    r = client.post(f"/queue/{qid}/close", data={"action": "handled"}, headers={"Origin": "http://attacker.example"})
    assert r.status_code == 403
    r = client.post(f"/queue/{qid}/close", data={"action": "handled"}, headers={"Origin": "null"})
    assert r.status_code == 403
    assert _status(tmp_path, qid) == "open"
    r = client.post(
        f"/queue/{qid}/close", data={"action": "handled"},
        headers={"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False,
    )
    assert r.status_code == 303 and _status(tmp_path, qid) == "handled"


def test_reads_are_not_checked_for_origin(tmp_path):
    client = make_client(tmp_path)
    assert client.get("/", headers={"Sec-Fetch-Site": "cross-site", "Origin": "http://x.example"}).status_code == 200


def test_trusted_hosts(tmp_path):
    hosts = ("127.0.0.1", "localhost", "[::1]", "::1")
    client = make_client(tmp_path, allowed_hosts=hosts, base_url="http://127.0.0.1:8321")
    assert client.get("/").status_code == 200
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 400
    assert client.get("/", headers={"Host": "localhost:8321"}).status_code == 200
    assert client.get("/", headers={"Host": "[::1]:8321"}).status_code == 200


def digest_answer(user):
    return BackendResult({"markdown": "## What's landing well\nAll good."}, 10, 5)


def test_one_dashboard_digest_at_a_time(tmp_path):
    client = make_client(tmp_path, llm_factory=fake_llm_factory(digest_answer))
    client.app.state.job_slots.digests = 1
    for path in ("/reports/digest", "/launch/1/digest"):
        r = client.post(path, follow_redirects=False)
        assert r.status_code == 409 and "A digest is already being written" in r.text
    client.app.state.job_slots.digests = 0
    assert client.post("/reports/digest", follow_redirects=False).status_code == 303
    assert client.app.state.job_slots.digests == 0  # released when the job finished


def test_at_most_two_investigations(tmp_path):
    backend = FakeBackend(step_handler=lambda t, a: StepResult("Done.", (), 10, 5))
    client = make_client(tmp_path, llm_factory=lambda conn, config: make_llm(conn, config, backend))
    client.app.state.job_slots.investigations = 2
    r = client.post("/investigations", data={"question": "why?"}, follow_redirects=False)
    assert r.status_code == 429 and "Two investigations are already running; try again in a minute" in r.text
    conn = connect(tmp_path / "pulse.db")
    assert conn.execute("SELECT COUNT(*) FROM investigations").fetchone()[0] == 0
    client.app.state.job_slots.investigations = 1
    assert client.post("/investigations", data={"question": "why?"}, follow_redirects=False).status_code == 303
    assert client.app.state.job_slots.investigations == 1


def test_failed_job_releases_its_slot(tmp_path):
    def broken(conn, config):
        raise RuntimeError("no key")

    client = make_client(tmp_path, llm_factory=broken, raise_server_exceptions=False)
    client.post("/investigations", data={"question": "why?"})
    client.post("/reports/digest")
    assert (client.app.state.job_slots.investigations, client.app.state.job_slots.digests) == (0, 0)
