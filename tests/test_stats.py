from dataclasses import replace
from datetime import timedelta

import pytest

from pulse import stats
from pulse.db import connect
from pulse.models import to_iso
from pulse.store import upsert_messages
from tests.fakes import T0, msg, set_triage

DAY = 24 * 60
NOW = T0 + timedelta(days=1)


def db(*items):
    """items: (message, sentiment, kind)"""
    conn = connect(":memory:")
    upsert_messages(conn, [m for m, _, _ in items], frozenset({"t1"}))
    for m, sentiment, kind in items:
        set_triage(conn, m.id, sentiment=sentiment, kind=kind)
    return conn


def theme(conn, name, *message_ids, status="active", merged_into=None):
    with conn:
        tid = conn.execute(
            "INSERT INTO themes (name, description, status, merged_into, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, f"{name} desc", status, merged_into, to_iso(T0)),
        ).lastrowid
        conn.executemany("INSERT INTO message_themes (message_id, theme_id) VALUES (?, ?)", [(m, tid) for m in message_ids])
    return tid


def test_period_summary_counts_only_community_messages():
    conn = db(
        (msg("a", minutes=0), -2, "bug"),
        (msg("b", minutes=1), 1, "praise"),
        (msg("s", minutes=2, author_id="t1"), 0, "other"),
    )
    s = stats.period_summary(conn, T0 - timedelta(hours=1), NOW)
    assert (s["messages"], s["negative"], s["avg_sentiment"]) == (2, 1, -0.5)
    assert s["by_kind"]["bug"] == 1 and s["by_kind"]["praise"] == 1 and s["by_kind"]["other"] == 0


def test_sentiment_series_fills_empty_days():
    conn = db((msg("a", minutes=0), -1, "bug"), (msg("b", minutes=2 * DAY), 1, "praise"))
    series = stats.sentiment_series(conn, T0.replace(hour=0), (T0 + timedelta(days=3)).replace(hour=0))
    assert [r["day"] for r in series] == ["2026-09-28", "2026-09-29", "2026-09-30"]
    assert [r["messages"] for r in series] == [1, 0, 1]
    assert series[1]["avg_sentiment"] is None


def test_theme_score_formula():
    conn = db(
        (msg("c1", minutes=0), -2, "bug"),
        (msg("c2", minutes=10), -1, "bug"),
        (msg("c3", minutes=20), 0, "question"),
        (msg("p1", minutes=-8 * DAY), -1, "bug"),
    )
    theme(conn, "Install", "c1", "c2", "c3", "p1")
    [s] = stats.theme_scores(conn, NOW)
    assert (s.volume, s.prev_volume) == (3, 1)
    assert s.mean_negativity == pytest.approx(1.0)
    assert s.trend == pytest.approx(2.0)
    assert s.score == pytest.approx(9.0)
    assert s.kinds == {"bug": 2, "question": 1}


def test_merged_theme_counts_toward_target_once():
    conn = db((msg("a", minutes=0), -2, "bug"), (msg("b", minutes=5), -2, "bug"))
    target = theme(conn, "Auth docs", "a")
    theme(conn, "Token exchange", "a", "b", status="merged", merged_into=target)
    [s] = stats.theme_scores(conn, NOW)
    assert (s.theme_id, s.name, s.volume) == (target, "Auth docs", 2)
    assert {m["message_id"] for m in stats.sample_messages(conn, T0 - timedelta(days=1), NOW, theme_id=target)} == {"a", "b"}


def test_theme_resolution_survives_cycles():
    conn = connect(":memory:")
    a = theme(conn, "A")
    b = theme(conn, "B", status="merged", merged_into=a)
    with conn:
        conn.execute("UPDATE themes SET status = 'merged', merged_into = ? WHERE id = ?", (b, a))
    resolved = stats.theme_resolution(conn)
    assert set(resolved) == {a, b}


def test_themes_with_no_recent_volume_are_excluded():
    conn = db((msg("old", minutes=-9 * DAY), -2, "bug"))
    theme(conn, "Old", "old")
    assert stats.theme_scores(conn, NOW) == []


def test_queue_counts():
    conn = db((msg("a"), -2, "bug"), (msg("b", minutes=1), 0, "question"))
    with conn:
        conn.execute("INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('a','a','frustrated','open','x')")
        conn.execute("INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('b','b','unanswered','open','x')")
        conn.execute("INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('c','b','unanswered','handled','x')")
    assert stats.queue_counts(conn) == {"open": 2, "frustrated": 1, "unanswered": 1}


def test_sample_messages_order_and_filters():
    conn = db(
        (msg("n", "ugh", minutes=0), -2, "bug"),
        (msg("p", "love it", minutes=1), 2, "praise"),
        (msg("q", "how?", minutes=2), 0, "question"),
    )
    start, end = T0 - timedelta(hours=1), NOW
    assert [m["message_id"] for m in stats.sample_messages(conn, start, end)] == ["n", "q", "p"]
    assert [m["message_id"] for m in stats.sample_messages(conn, start, end, most_negative=False)] == ["p", "q", "n"]
    assert [m["message_id"] for m in stats.sample_messages(conn, start, end, kinds=("praise",))] == ["p"]
    first = stats.sample_messages(conn, start, end, limit=1)[0]
    assert set(first) == {"message_id", "author", "channel", "created_at", "kind", "sentiment", "content"}
    assert stats.sample_messages(conn, start, end, theme_id=999) == []


def test_theme_scores_with_explicit_start_uses_equal_previous_window():
    conn = db(
        (msg("cur", minutes=0), -1, "bug"),
        (msg("prev", minutes=-2 * DAY), -1, "bug"),
        (msg("older", minutes=-5 * DAY), -1, "bug"),
    )
    theme(conn, "Install", "cur", "prev", "older")
    start = T0 - timedelta(hours=12)
    [s] = stats.theme_scores(conn, start + timedelta(days=3), start=start, window_days=99)
    assert (s.volume, s.prev_volume) == (1, 1)


def test_like_escapes_wildcards():
    conn = db((msg("a", "100% done"), 0, "other"), (msg("b", "1000 done"), 0, "other"), (msg("c", "a_b"), 0, "other"),
              (msg("d", "axb"), 0, "other"), (msg("e", r"back\slash"), 0, "other"))

    def hits(text):
        return [r[0] for r in conn.execute(
            "SELECT id FROM messages WHERE lower(content) LIKE ? ESCAPE '\\' ORDER BY id", (stats._like(text),)
        )]

    assert hits("100%") == ["a"]
    assert hits("a_b") == ["c"]
    assert hits("k\\s") == ["e"]


NAMES = {"100": "help", "200": "general", "300": "M1 install thread", "601": "forum post a", "602": "forum post b"}


def _m(id, channel, sentiment, *, minutes=0, thread=None, parent=None):
    m = replace(
        msg(id, f"text {id}", minutes=minutes, channel_id=channel, thread_id=thread),
        channel_name=NAMES[channel], parent_channel_id=parent,
    )
    return m, sentiment


def _channel_db():
    conn = connect(":memory:")
    rows = [
        _m("h1", "100", -2, minutes=0),
        _m("h2", "100", -1, minutes=1),
        _m("t1", "300", -2, minutes=2, thread="300", parent="100"),
        _m("g1", "200", 2, minutes=3),
    ]
    upsert_messages(conn, [m for m, _ in rows], frozenset())
    for m, s in rows:
        set_triage(conn, m.id, sentiment=s)
    return conn


START, END = T0 - timedelta(hours=1), T0 + timedelta(hours=1)


def test_channel_scope_includes_threads_of_the_channel():
    conn = _channel_db()
    help_ = stats.period_summary(conn, START, END, channels=("100",))
    assert help_["messages"] == 3 and help_["negative"] == 3
    assert stats.period_summary(conn, START, END, channels=("200",))["messages"] == 1
    assert stats.period_summary(conn, START, END)["messages"] == 4


def test_series_and_samples_respect_channel_scope():
    conn = _channel_db()
    day = T0.replace(hour=0)
    series = stats.sentiment_series(conn, day, day + timedelta(days=1), channels=("200",))
    assert series == [{"day": "2026-09-28", "messages": 1, "avg_sentiment": 2.0}]
    ids = {m["message_id"] for m in stats.sample_messages(conn, START, END, channels=("100",), limit=10)}
    assert ids == {"h1", "h2", "t1"}


def test_theme_scores_respect_channel_scope():
    conn = _channel_db()
    with conn:
        conn.execute("INSERT INTO themes (id, name, created_at) VALUES (1, 'install', ?)", (to_iso(T0),))
        for mid in ("h1", "t1", "g1"):
            conn.execute("INSERT INTO message_themes (message_id, theme_id) VALUES (?, 1)", (mid,))
    now = T0 + timedelta(hours=1)
    assert stats.theme_scores(conn, now, channels=("100",))[0].volume == 2
    assert stats.theme_scores(conn, now, channels=("200",))[0].volume == 1
    assert stats.theme_scores(conn, now)[0].volume == 3


def test_top_channels_lists_top_level_channels_busiest_first():
    conn = _channel_db()
    assert stats.top_channels(conn) == [
        {"id": "100", "name": "help", "messages": 3},  # its thread counts toward it
        {"id": "200", "name": "general", "messages": 1},
    ]


def _forum_db():
    """Forum channel 600 has only threads (601, 602); #general (200) has top-level posts."""
    conn = connect(":memory:")
    rows = [
        _m("f1", "601", -2, minutes=0, thread="601", parent="600"),
        _m("f2", "601", -1, minutes=1, thread="601", parent="600"),
        _m("f3", "602", -1, minutes=2, thread="602", parent="600"),
        _m("g1", "200", 2, minutes=3),
    ]
    upsert_messages(conn, [m for m, _ in rows], frozenset())
    for m, s in rows:
        set_triage(conn, m.id, sentiment=s)
    return conn


def test_top_channels_include_thread_only_forum_channels():
    conn = _forum_db()
    assert stats.top_channels(conn) == [
        {"id": "600", "name": "600", "messages": 3},
        {"id": "200", "name": "general", "messages": 1},
    ]
    rows = stats.channel_breakdown(conn, START, END, END)
    assert [(r["id"], r["messages"]) for r in rows] == [("600", 3), ("200", 1)]


def _reply_db():
    """Questions in #help (100), its thread 300, and #general (200); staff author id is t1."""
    conn = connect(":memory:")
    team = frozenset({"t1"})
    rows = [
        replace(msg("q1", "how do I log in?", minutes=0, channel_id="100"), channel_name="help"),
        replace(msg("s1", "use exchange_token", minutes=30, channel_id="100", author_id="t1",
                    author_name="staff", reply_to_id="q1"), channel_name="help"),
        replace(msg("q2", "wheel error", minutes=10, channel_id="300", thread_id="300"),
                channel_name="M1 thread", parent_channel_id="100"),
        replace(msg("s2", "fixed in 2.0.1", minutes=70, channel_id="300", thread_id="300", author_id="t1",
                    author_name="staff"), channel_name="M1 thread", parent_channel_id="100"),
        replace(msg("q3", "docs 404?", minutes=20, channel_id="100"), channel_name="help"),
        replace(msg("s3", "fixed", minutes=40, channel_id="q3", thread_id="q3", author_id="t1",
                    author_name="staff"), channel_name="docs 404?", parent_channel_id="100"),
        replace(msg("q4", "429s again", minutes=60, channel_id="200"), channel_name="general"),
        replace(msg("q5", "still broken", minutes=20 * 60, channel_id="200"), channel_name="general"),
        replace(msg("s0", "early staff note", minutes=-5, channel_id="200", author_id="t1",
                    author_name="staff", reply_to_id="q4"), channel_name="general"),
        replace(msg("c1", "nice release", minutes=5, channel_id="200"), channel_name="general"),
    ]
    upsert_messages(conn, rows, team)
    for mid in ("q1", "q2", "q3", "q4", "q5"):
        set_triage(conn, mid, sentiment=-1, needs_reply=True)
    set_triage(conn, "c1", sentiment=2, needs_reply=False, kind="praise")
    return conn


REPLY_START, REPLY_NOW = T0 - timedelta(days=1), T0 + timedelta(hours=30)


def test_reply_stats_median_and_waiting():
    conn = _reply_db()
    r = stats.reply_stats(conn, REPLY_START, REPLY_NOW, REPLY_NOW)
    # q1 30 min (reply), q2 60 min (same thread), q3 20 min (thread started from it);
    # q4 unanswered for 29 h (the staff note came before it); q5 unanswered for 10 h.
    assert r == {"needs_reply": 5, "answered": 3, "median_minutes": 30.0, "waiting_over_24h": 1}


def test_reply_stats_with_no_questions():
    conn = connect(":memory:")
    assert stats.reply_stats(conn, REPLY_START, REPLY_NOW, REPLY_NOW) == {
        "needs_reply": 0, "answered": 0, "median_minutes": None, "waiting_over_24h": 0,
    }


def test_reply_stats_respect_channel_scope():
    conn = _reply_db()
    help_ = stats.reply_stats(conn, REPLY_START, REPLY_NOW, REPLY_NOW, channels=("100",))
    assert help_ == {"needs_reply": 3, "answered": 3, "median_minutes": 30.0, "waiting_over_24h": 0}


def test_channel_breakdown_per_top_level_channel():
    conn = _reply_db()
    rows = stats.channel_breakdown(conn, REPLY_START, REPLY_NOW, REPLY_NOW)
    assert [r["id"] for r in rows] == ["100", "200"]
    help_, general = rows
    assert help_ == {
        "id": "100", "name": "help", "messages": 3, "avg_sentiment": -1.0, "negative_share": 1.0,
        "needs_reply": 3, "median_reply_minutes": 30.0, "waiting_over_24h": 0,
    }
    assert general["messages"] == 3 and general["needs_reply"] == 2 and general["waiting_over_24h"] == 1
    assert general["median_reply_minutes"] is None


def test_period_summary_and_series_scope_by_theme():
    conn = _channel_db()
    with conn:
        conn.execute("INSERT INTO themes (id, name, created_at) VALUES (1, 'install', ?)", (to_iso(T0),))
        conn.execute("INSERT INTO themes (id, name, status, merged_into, created_at) VALUES (2, 'm1', 'merged', 1, ?)", (to_iso(T0),))
        conn.execute("INSERT INTO message_themes (message_id, theme_id) VALUES ('h1', 1), ('g1', 2)")
    s = stats.period_summary(conn, START, END, theme_id=1)
    assert s["messages"] == 2  # g1 is in a theme merged into 1
    assert stats.period_summary(conn, START, END, theme_id=1, channels=("200",))["messages"] == 1
    assert stats.period_summary(conn, START, END, theme_id=99)["messages"] == 0
    day = T0.replace(hour=0)
    assert stats.sentiment_series(conn, day, day + timedelta(days=1), theme_id=1)[0]["messages"] == 2


def test_reply_after_now_is_not_answered():
    conn = connect(":memory:")
    team = frozenset({"t1"})
    rows = [
        replace(msg("q", "help", minutes=0, channel_id="100"), channel_name="help"),
        replace(msg("s", "answer", minutes=3 * 24 * 60, channel_id="100", author_id="t1",
                    author_name="staff", reply_to_id="q"), channel_name="help"),
    ]
    upsert_messages(conn, rows, team)
    set_triage(conn, "q", sentiment=-1, needs_reply=True)
    r = stats.reply_stats(conn, REPLY_START, T0 + timedelta(days=2), T0 + timedelta(days=2))
    assert r == {"needs_reply": 1, "answered": 0, "median_minutes": None, "waiting_over_24h": 1}
