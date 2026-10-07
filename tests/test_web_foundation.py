from dataclasses import replace
from datetime import timedelta

from pulse.citations import render_html
from pulse.db import connect
from pulse.store import upsert_messages
from pulse.web.cards import cards_by_ids
from pulse.web.filters import parse_filters
from tests.fakes import make_config, msg
from tests.web_fakes import NOW, make_client, seed


def test_static_css_is_served(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/static/pulse.css")
    assert r.status_code == 200
    assert "--accent" in r.text and "nav a{" in r.text


def test_parse_filters_falls_back():
    f = parse_filters("abc", "999", NOW, {"100"})
    assert (f.days, f.channel, f.channels) == (14, None, None)
    assert parse_filters("5", None, NOW, set()).days == 14
    f = parse_filters("30", "100", NOW, {"100"})
    assert (f.days, f.channel, f.channels) == (30, "100", ("100",))
    assert f.start == NOW - timedelta(days=30) and f.end == NOW


def test_filters_qs_keeps_channel_and_applies_overrides():
    f = parse_filters("7", "100", NOW, {"100"})
    assert f.qs() == "?days=7&channel=100"
    assert f.qs(days=30, theme=2) == "?days=30&channel=100&theme=2"
    assert parse_filters(None, None, NOW, set()).qs(author_id="") == "?days=14"


def test_render_html_escapes_raw_html_and_links_citations(tmp_path):
    conn = connect(tmp_path / "x.db")
    seed(conn)
    html = render_html("Hi <img src=x onerror=alert(1)> [[msg:q1]] and [[msg:nope]]\n\n- item", conn)
    assert "<img" not in html and "&lt;img" in html
    assert 'href="https://discord.com/channels/900/100/q1"' in html and "@alice" in html
    assert "[missing message]" in html
    assert "<li>item</li>" in html


def _macro(client):
    return client.app.state.templates.env.get_template("_macros.html").module.message_card


def test_message_card_escapes_content_and_links(tmp_path):
    client = make_client(tmp_path)
    card = cards_by_ids(connect(tmp_path / "pulse.db"), ["q1"])[0]
    html = str(_macro(client)(card, NOW, parse_filters(None, None, NOW, set())))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "https://discord.com/channels/900/100/q1" in html
    assert "/messages?days=14&amp;author_id=u1" in html
    assert "All from alice" in html and "2d ago" in html and "#help" in html


def test_message_card_team_pill_and_long_text(tmp_path):
    client = make_client(tmp_path)
    card = cards_by_ids(connect(tmp_path / "pulse.db"), ["s1"])[0]
    f = parse_filters(None, None, NOW, set())
    html = str(_macro(client)(card, NOW, f))
    assert 'class="pill team"' in html
    long = str(_macro(client)({**card, "content": "x" * 450}, NOW, f))
    assert "Show all" in long


def test_cards_by_ids_keeps_order_and_skips_unknown(tmp_path):
    conn = connect(tmp_path / "x.db")
    seed(conn)
    assert [c["message_id"] for c in cards_by_ids(conn, ["g2", "nope", "q1", "g2"])] == ["g2", "q1"]


def test_layout_renders_on_empty_db_with_bad_params(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/?days=abc&channel=zzz")
    assert r.status_code == 200
    assert "Community pulse" in r.text and "Acme SDK Community" in r.text
    assert 'aria-current="page"' in r.text


def test_layout_shows_budget_banner_when_cap_reached(tmp_path):
    r = make_client(tmp_path, config=make_config(daily_usd_cap=0.01)).get("/")
    assert "reached the $0.01 daily cap" in r.text


def test_layout_shows_coverage_banner_only_when_messages_are_untriaged(tmp_path):
    client = make_client(tmp_path)
    assert "community messages in this view" not in client.get("/").text
    conn = connect(tmp_path / "pulse.db")
    upsert_messages(conn, [replace(msg("late", "not triaged yet", channel_id="200"),
                                   created_at=NOW - timedelta(hours=1))], frozenset({"t1"}))
    conn.close()
    total = client.get("/").text
    assert "Analyzed" in total and "community messages in this view" in total
    assert "community messages in this view" not in client.get("/?channel=100").text


def test_nav_counts_follow_channel_filter(tmp_path):
    client = make_client(tmp_path)
    assert '<span class="count hot">3</span>' in client.get("/").text
    assert '<span class="count hot">1</span>' in client.get("/?channel=200").text
