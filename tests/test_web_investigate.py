import json
from datetime import timedelta

from pulse.agents.base import ProviderError, StepResult
from pulse.db import connect
from pulse.models import to_iso
from tests.fakes import FakeBackend, make_llm
from tests.web_fakes import NOW, make_client


def factory_with(step_handler):
    backend = FakeBackend(step_handler=step_handler)
    return lambda conn, config: make_llm(conn, config, backend)


def answer(transcript, allow_tools):
    return StepResult("Mostly M1 installs.", (), 10, 5)


def test_start_investigation_from_pain_point(tmp_path):
    client = make_client(tmp_path, llm_factory=factory_with(answer))
    r = client.post("/investigations?days=14", data={"question": "Why are installs failing?", "theme_id": "1"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/reports/investigation/1?days=14"
    page = client.get(r.headers["location"]).text
    assert "Mostly M1 installs." in page and "hx-trigger" not in page
    conn = connect(tmp_path / "pulse.db")
    assert json.loads(conn.execute("SELECT context FROM investigations WHERE id = 1").fetchone()[0]) == {
        "theme_id": 1, "days": 14,
    }


def test_failed_investigation_stops_polling(tmp_path):
    client = make_client(tmp_path, llm_factory=factory_with(lambda t, a: ProviderError("bad model")))
    page = client.post("/investigations", data={"question": "why?"}).text
    assert "Investigation failed" in page and "hx-trigger" not in page


def test_factory_error_marks_investigation_failed(tmp_path):
    def broken(conn, config):
        raise RuntimeError("no key")

    page = make_client(tmp_path, llm_factory=broken).post("/investigations", data={"question": "why?"}).text
    assert "Investigation failed: RuntimeError: no key" in page


def test_investigation_input_validation(tmp_path):
    client = make_client(tmp_path, llm_factory=factory_with(answer))
    assert client.post("/investigations", data={"question": "  "}).status_code == 400
    assert client.post("/investigations", data={"question": "x" * 501}).status_code == 400
    assert client.post("/investigations", data={"question": "why?", "theme_id": "abc"}).status_code == 400
    assert client.post("/investigations", data={"question": "why?", "theme_id": "999"}).status_code == 404
    assert client.post("/investigations", data={"question": "why?", "launch": "nope"}).status_code == 404
    (tmp_path / "off").mkdir()
    off = make_client(tmp_path / "off", demo=True, llm_factory=factory_with(answer))
    assert off.post("/investigations", data={"question": "why?"}).status_code == 409


def test_investigate_buttons_render(tmp_path):
    on = make_client(tmp_path, llm_factory=factory_with(answer))
    pain = on.get("/pain").text
    assert 'action="/investigations?days=14"' in pain and 'name="theme_id" value="1"' in pain
    assert "Ask why sentiment changed" in on.get("/").text
    assert "Ask about this launch" in on.get("/launch").text
    (tmp_path / "off").mkdir()
    off = make_client(tmp_path / "off").get("/pain").text
    assert "Agents are off" in off


def test_stale_running_investigation_shows_timeout(tmp_path):
    client = make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    with conn:
        conn.execute("INSERT INTO investigations (id, question, context, created_at) VALUES (5, 'why?', '{}', ?)",
                     (to_iso(NOW - timedelta(minutes=11)),))
    page = client.get("/reports/investigation/5").text
    assert "No result after 10 minutes" in page and "hx-trigger" not in page
