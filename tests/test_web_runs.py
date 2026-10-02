from tests.web_fakes import make_client


def test_runs_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/runs")
    assert r.status_code == 200 and "No agent runs yet" in r.text and "$0.00" in r.text


def test_runs_show_spend_daily_costs_and_errors(tmp_path):
    html = make_client(tmp_path).get("/runs").text
    assert "$0.04" in html and "of $5.00" in html
    assert "2026-09-30" in html  # today's row in the daily table
    assert "provider 500" in html and "triage" in html and "digest" in html


def test_runs_status_filter(tmp_path):
    client = make_client(tmp_path)
    html = client.get("/runs?status=failed").text
    assert "provider 500" in html and "anthropic:m-triage" not in html
    assert client.get("/runs?status=bogus").status_code == 200
