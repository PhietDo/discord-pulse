from dataclasses import replace

import httpx

from pulse.config import IntegrationsConfig
from tests.web_fakes import CONFIG, make_client

GH = replace(CONFIG, integrations=IntegrationsConfig(github_repo="acme/sdk", linear_team_id="team-1"))


def github(status=201, body=None):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body or {"html_url": "https://github.com/acme/sdk/issues/12", "number": 12})

    return calls, (lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_no_tracker_controls_without_integrations(tmp_path):
    assert "Send to" not in make_client(tmp_path).get("/pain").text


def test_controls_show_ready_and_disabled_trackers(tmp_path):
    html = make_client(tmp_path, config=GH, env={"GITHUB_TOKEN": "x"}).get("/pain?theme=1").text
    assert "Send to GitHub" in html and "no author names" in html and "acme/sdk" in html
    assert "Set LINEAR_API_KEY to send issues to Linear" in html


def test_send_creates_once_then_links(tmp_path):
    calls, factory = github()
    client = make_client(tmp_path, config=GH, env={"GITHUB_TOKEN": "x"}, http_client_factory=factory)
    r = client.post("/pain/1/issue", data={"tracker": "github"}, follow_redirects=False)
    assert r.status_code == 303 and "theme=1" in r.headers["location"] and "issued=created" in r.headers["location"]
    html = client.get(r.headers["location"]).text
    assert 'href="https://github.com/acme/sdk/issues/12"' in html and "GitHub issue #12" in html
    assert "Issue created." in html
    r2 = client.post("/pain/1/issue", data={"tracker": "github"}, follow_redirects=False)
    assert "issued=exists" in r2.headers["location"] and len(calls) == 1


def test_send_errors(tmp_path):
    _, factory = github(404, {"message": "Not Found"})
    client = make_client(tmp_path, config=GH, env={"GITHUB_TOKEN": "x"}, http_client_factory=factory)
    r = client.post("/pain/1/issue", data={"tracker": "github"})
    assert r.status_code == 502 and "GitHub returned 404: Not Found" in r.text
    assert client.post("/pain/1/issue", data={"tracker": "jira"}).status_code == 400
    assert client.post("/pain/999/issue", data={"tracker": "github"}).status_code == 404
    cross = client.post("/pain/1/issue", data={"tracker": "github"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
