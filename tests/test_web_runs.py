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


def test_daily_table_splits_failed_and_skipped(tmp_path):
    from datetime import timedelta

    from pulse.db import connect
    from pulse.models import to_iso
    from pulse.web.queries import daily_costs
    from tests.web_fakes import NOW

    client = make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    with conn:
        for _ in range(2):
            conn.execute(
                "INSERT INTO agent_runs (agent, model, cost_usd, status, started_at, finished_at)"
                " VALUES ('theme', 'anthropic:m', 0.0, 'skipped_budget', ?, ?)",
                (to_iso(NOW - timedelta(hours=1)), to_iso(NOW - timedelta(hours=1))),
            )
    today = daily_costs(conn, NOW)[-1]
    assert (today["runs"], today["failed"], today["skipped"]) == (4, 1, 2)
    html = client.get("/runs").text
    assert '<th class="num">Skipped</th>' in html
    assert '<td class="num neg">1</td><td class="num warnc">2</td>' in html
