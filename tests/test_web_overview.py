from tests.web_fakes import make_client


def test_overview_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/")
    assert r.status_code == 200
    assert "No messages in this window" in r.text
    assert "No pain points for this filter yet" in r.text
    assert "queue is clear" in r.text
    assert "Nothing is waiting for a reply" in r.text


def test_overview_shows_stats_chart_pain_points_and_cards(tmp_path):
    html = make_client(tmp_path).get("/").text
    assert ">6</span>" in html  # messages in the last 7 days
    assert "30 min" in html and "1 of 4 answered · 3 waiting &gt;24h" in html
    assert "<svg class=\"chart\"" in html and "v2.0" in html
    assert "Install fails on M1" in html and "/pain?days=14&amp;theme=1" in html
    assert "https://discord.com/channels/900/200/g2" in html  # needs attention card
    assert "uma" in html and "honestly v2 is the best release yet" in html  # landing well
    assert 'class="cite"' in html and "@uma" in html  # latest digest excerpt


def test_overview_channel_filter_recomputes(tmp_path):
    html = make_client(tmp_path).get("/?channel=200").text
    assert "#general" in html and "/?days=14&amp;channel=100" not in html
    assert "No pain points for this filter yet" in html
    assert 'id="msg-q1"' not in html and 'id="msg-g2"' in html  # only #general cards
