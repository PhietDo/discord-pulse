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


def _add_forum_threads(tmp_path):
    from dataclasses import replace

    from pulse.db import connect
    from pulse.store import upsert_messages
    from tests.fakes import msg, set_triage

    conn = connect(tmp_path / "pulse.db")
    rows = [
        replace(msg(f"f{i}", "forum: sdk crashes on start", minutes=30 + i, channel_id=tid, thread_id=tid,
                    author_id="u9", author_name="fay"), channel_name=f"post {tid}", parent_channel_id="600")
        for i, tid in enumerate(("601", "601", "602"))
    ]
    upsert_messages(conn, rows, frozenset({"t1"}))
    for r in rows:
        set_triage(conn, r.id, sentiment=-1, kind="bug")
    conn.close()


def test_thread_only_forum_channel_is_a_channel(tmp_path):
    client = make_client(tmp_path)
    _add_forum_threads(tmp_path)
    html = client.get("/?channel=600").text
    assert '<option value="600" selected>' in html  # honored, not dropped to all channels
    assert 'id="msg-q1"' not in html
    assert '<td class="where"><a href="/?days=14&amp;channel=600">#600</a></td>' in html  # breakdown row


def _add_open_items(tmp_path, n):
    """n extra open 'unanswered' queue items in #general, inserted directly."""
    from dataclasses import replace

    from pulse.db import connect
    from pulse.models import to_iso
    from pulse.store import upsert_messages
    from tests.fakes import msg
    from tests.web_fakes import NOW

    conn = connect(tmp_path / "pulse.db")
    rows = [replace(msg(f"x{i}", f"question {i}", minutes=300 + i, channel_id="200", author_id="u8",
                        author_name="xavi"), channel_name="general") for i in range(n)]
    upsert_messages(conn, rows, frozenset({"t1"}))
    with conn:
        conn.executemany(
            "INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at)"
            " VALUES (?, ?, 'unanswered', 'open', ?)",
            [(r.id, r.id, to_iso(NOW)) for r in rows],
        )
    total = conn.execute("SELECT COUNT(*) FROM mod_queue WHERE status = 'open'").fetchone()[0]
    conn.close()
    return total


def test_queue_counts_are_not_truncated(tmp_path):
    client = make_client(tmp_path)
    total = _add_open_items(tmp_path, 227)
    assert total == 230
    html = client.get("/").text
    assert '<span class="big neg">230</span>' in html
    assert "2 frustrated · oldest 2d ago" in html
    queue = client.get("/queue").text
    assert "Showing 200 of 230" in queue
    assert "Showing" not in client.get("/queue?reason=frustrated").text


def test_overview_stat_strip_says_which_are_last_7_days(tmp_path):
    html = make_client(tmp_path).get("/?days=30").text
    assert "Last 30 days" in html
    assert "Avg sentiment, last 7 days" in html and "First staff reply, last 7 days" in html


def test_channel_breakdown_only_computes_requested_channels(tmp_path):
    from pulse import stats
    from pulse.db import connect
    from tests.web_fakes import NOW

    make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    start = NOW - __import__("datetime").timedelta(days=14)
    assert [r["id"] for r in stats.channel_breakdown(conn, start, NOW, NOW, channels=("200",))] == ["200"]
    assert [r["id"] for r in stats.channel_breakdown(conn, start, NOW, NOW)] == ["100", "200"]
