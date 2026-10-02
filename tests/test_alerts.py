import json
from datetime import timedelta

import httpx
import pytest

from pulse.alerts import MAX_PER_RUN, find_alerts, send_alerts
from pulse.config import AlertsConfig
from pulse.db import connect
from pulse.models import to_iso
from pulse.pipeline import format_alerts
from pulse.store import upsert_messages
from pulse.themes import assign, create_theme
from tests.fakes import T0, make_config, msg, set_triage

NOW = T0 + timedelta(days=1)
CFG = make_config(alerts=AlertsConfig(enabled=True))


def seed(spike=6, frustrated_age_hours=13, staff_reply=False):
    conn = connect(":memory:")
    base = 24 * 60  # NOW is T0 + 1 day, in minutes from T0
    msgs = [msg(f"s{i}", f"deploy hangs <again> & again {i}", minutes=base - 60 - i, author_id=f"u{i}")
            for i in range(spike)]
    msgs.append(msg("f1", "this is the THIRD outage <b>today</b>", minutes=base - frustrated_age_hours * 60,
                    author_id="u9", author_name="erin"))
    if staff_reply:
        msgs.append(msg("r1", "looking into it", minutes=base - 30, author_id="t1", author_name="sam",
                        reply_to_id="f1"))
    upsert_messages(conn, msgs, frozenset({"t1"}))
    for m in msgs:
        set_triage(conn, m.id, sentiment=-2 if m.id == "f1" else -1, kind="bug", topics=("deploy",))
    with conn:
        tid, _ = create_theme(conn, "WSL deploy hangs", "", T0)
        for m in msgs[:spike]:
            assign(conn, m.id, tid)
        conn.execute(
            "INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('f1', 'f1',"
            " 'frustrated', 'open', ?)", (to_iso(NOW),))
    return conn


def slack(status=200):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, text="ok")

    return calls, httpx.Client(transport=httpx.MockTransport(handler))


def test_find_alerts_spike_and_frustrated_with_escaping():
    alerts = find_alerts(seed(), CFG, NOW)
    assert [a.kind for a in alerts] == ["spike", "frustrated"]
    spike, frus = alerts
    assert spike.key == f"1:{NOW.date().isoformat()}"
    assert "*WSL deploy hangs*: 6 messages in the last 24 hours (previous 24 hours: 0)" in spike.text
    assert "&lt;again&gt; &amp; again" in spike.text and "<again>" not in spike.text
    assert "|Open in Discord>" in spike.text
    assert frus.key == "1" and "unanswered for 13 hours: erin in #help" in frus.text
    assert "&lt;b&gt;today&lt;/b&gt;" in frus.text


def test_thresholds_and_staff_replies_suppress_alerts():
    assert find_alerts(seed(spike=4), CFG, NOW)[0].kind == "frustrated"   # below spike_min_volume
    assert [a.kind for a in find_alerts(seed(frustrated_age_hours=6), CFG, NOW)] == ["spike"]
    assert [a.kind for a in find_alerts(seed(staff_reply=True), CFG, NOW)] == ["spike"]


def test_send_alerts_posts_once_and_records():
    conn = seed()
    calls, client = slack()
    stats = send_alerts(conn, CFG, NOW, client=client, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"})
    assert (stats.found, stats.sent, stats.failed) == (2, 2, 0) and len(calls) == 2
    again = send_alerts(conn, CFG, NOW + timedelta(minutes=30), client=client,
                        env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"})
    assert (again.found, again.sent) == (0, 0) and len(calls) == 2
    assert format_alerts(stats) == "alerts: 2 found, 2 sent"


def test_failed_post_is_retried_next_run():
    conn = seed()
    _, bad = slack(500)
    stats = send_alerts(conn, CFG, NOW, client=bad, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"})
    assert (stats.sent, stats.failed) == (0, 1)
    assert format_alerts(stats) == "alerts: 2 found, 0 sent, 1 failed (will retry)"
    calls, good = slack()
    assert send_alerts(conn, CFG, NOW, client=good, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"}).sent == 2


def test_missing_webhook_skips_without_error():
    stats = send_alerts(seed(), CFG, NOW, env={})
    assert stats.found == 2 and stats.sent == 0 and stats.skipped == "SLACK_WEBHOOK_URL is not set"
    assert "https://" not in format_alerts(stats)


def test_at_most_ten_alerts_per_run(monkeypatch):
    from pulse import alerts as mod
    from pulse.alerts import Alert

    monkeypatch.setattr(mod, "find_alerts", lambda c, cfg, now: [Alert("frustrated", str(i), "x") for i in range(15)])
    calls, client = slack()
    assert send_alerts(seed(), CFG, NOW, client=client, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"}).sent == MAX_PER_RUN == 10


def test_alerts_config_and_cli_dry_run(tmp_path, monkeypatch, capsys):
    from pulse.config import ConfigError, load_config
    from pulse.run import main
    from tests.test_cli import CONFIG

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    path = tmp_path / "pulse.toml"
    path.write_text(CONFIG + "\n[alerts]\nenabled = true\nspike_min_volume = 3\n")
    assert load_config(path).alerts == AlertsConfig(enabled=True, spike_min_volume=3)
    assert main(["--config", str(path), "alerts", "--dry-run"]) == 0
    assert "no alerts" in capsys.readouterr().out
    path.write_text(CONFIG + '\n[alerts]\nenabled = "yes"\n')
    with pytest.raises(ConfigError, match="enabled"):
        load_config(path)


@pytest.mark.parametrize("field", ["spike_min_volume", "spike_trend", "frustrated_hours"])
def test_alerts_config_rejects_bool_numeric_fields(tmp_path, monkeypatch, field):
    from pulse.config import ConfigError, load_config
    from tests.test_cli import CONFIG

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    path = tmp_path / "pulse.toml"
    path.write_text(CONFIG + f"\n[alerts]\nenabled = true\n{field} = true\n")
    with pytest.raises(ConfigError, match="not true/false"):
        load_config(path)


def test_frustrated_alerts_ignore_items_older_than_the_lookback():
    assert [a.kind for a in find_alerts(seed(frustrated_age_hours=240), CFG, NOW)] == ["spike"]
    assert [a.kind for a in find_alerts(seed(frustrated_age_hours=13), CFG, NOW)] == ["spike", "frustrated"]
