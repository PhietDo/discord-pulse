from datetime import timedelta

from pulse.db import connect
from pulse.models import to_iso
from pulse.modqueue import refresh_mod_queue
from pulse.store import upsert_messages
from tests.fakes import T0, make_config, msg, set_triage

CONFIG = make_config()  # team = {"t1"}, window 12h, threshold -2
NOW = T0 + timedelta(hours=24)


def db(*messages):
    conn = connect(":memory:")
    upsert_messages(conn, messages, CONFIG.team_member_ids)
    return conn


def items(conn):
    return conn.execute("SELECT * FROM mod_queue ORDER BY id").fetchall()


def test_unanswered_question_past_window_is_queued():
    conn = db(msg("q1", "how do I auth?"))
    set_triage(conn, "q1", needs_reply=True)
    stats = refresh_mod_queue(conn, CONFIG, NOW)
    assert stats.opened == 1
    [item] = items(conn)
    assert (item["message_id"], item["reason"], item["status"], item["queue_key"]) == ("q1", "unanswered", "open", "q1")


def test_question_inside_window_is_not_queued_yet():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, T0 + timedelta(hours=2)).opened == 0


def test_direct_team_reply_prevents_queueing():
    conn = db(msg("q1"), msg("r1", minutes=30, author_id="t1", reply_to_id="q1"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 0


def test_team_message_in_same_thread_prevents_queueing():
    conn = db(msg("q1", thread_id="T"), msg("r1", minutes=30, author_id="t1", thread_id="T"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 0


def test_non_team_reply_does_not_count():
    conn = db(msg("q1"), msg("r1", minutes=30, author_id="u2", reply_to_id="q1"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 1


def test_frustrated_is_queued_even_if_young_and_answered():
    conn = db(msg("f1", "unusable"), msg("r1", minutes=5, author_id="t1", reply_to_id="f1"))
    set_triage(conn, "f1", sentiment=-2)
    refresh_mod_queue(conn, CONFIG, T0 + timedelta(minutes=10))
    [item] = items(conn)
    assert item["reason"] == "frustrated"


def test_team_messages_are_never_queued():
    conn = db(msg("t", "our fault, sorry", author_id="t1"))
    set_triage(conn, "t", sentiment=-2, needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 0


def test_one_open_item_per_thread_moves_to_later_trigger():
    conn = db(msg("a", thread_id="T"), msg("b", minutes=600, thread_id="T"))
    set_triage(conn, "a", needs_reply=True)
    set_triage(conn, "b", sentiment=-2)
    stats = refresh_mod_queue(conn, CONFIG, NOW)
    assert (stats.opened, stats.updated) == (1, 1)
    [item] = items(conn)
    assert (item["message_id"], item["reason"], item["queue_key"]) == ("b", "frustrated", "T")


def test_rerun_is_idempotent():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    refresh_mod_queue(conn, CONFIG, NOW)
    stats = refresh_mod_queue(conn, CONFIG, NOW + timedelta(minutes=30))
    assert (stats.opened, stats.updated) == (0, 0)
    assert len(items(conn)) == 1


def test_unanswered_auto_closes_when_team_replies_later():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    refresh_mod_queue(conn, CONFIG, NOW)
    upsert_messages(conn, [msg("r1", minutes=60 * 25, author_id="t1", reply_to_id="q1")], CONFIG.team_member_ids)
    stats = refresh_mod_queue(conn, CONFIG, NOW + timedelta(hours=2))
    assert stats.auto_closed == 1
    [item] = items(conn)
    assert (item["status"], item["closed_by"]) == ("handled", "auto")


def test_dismissed_item_is_not_reopened():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    refresh_mod_queue(conn, CONFIG, NOW)
    with conn:
        conn.execute(
            "UPDATE mod_queue SET status = 'dismissed', closed_at = ?, closed_by = 'user'", (to_iso(NOW),)
        )
    assert refresh_mod_queue(conn, CONFIG, NOW + timedelta(hours=1)).opened == 0


def test_messages_older_than_lookback_are_ignored():
    conn = db(msg("old", minutes=0))
    set_triage(conn, "old", sentiment=-2, needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, T0 + timedelta(days=8)).opened == 0


def test_team_reply_in_thread_started_from_question_counts():
    conn = db(msg("q1"), msg("r1", minutes=30, author_id="t1", thread_id="q1"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 0


from pulse.modqueue import list_open


def set_p(conn, message_id, p):
    with conn:
        conn.execute("UPDATE triage SET needs_reply_p = ? WHERE message_id = ?", (p, message_id))


def test_list_open_orders_frustrated_then_probability_then_age():
    conn = db(msg("a", minutes=0), msg("b", minutes=10), msg("c", minutes=20), msg("d", minutes=30))
    for mid in ("a", "b", "c"):
        set_triage(conn, mid, needs_reply=True)
    set_triage(conn, "d", sentiment=-2)
    set_p(conn, "a", 0.72)
    set_p(conn, "b", 0.95)
    # c has no probability (LLM-only row with needs_reply=1), so it ranks as 1.0
    refresh_mod_queue(conn, CONFIG, NOW)
    assert [r["message_id"] for r in list_open(conn)] == ["d", "c", "b", "a"]
    assert len(list_open(conn, limit=2)) == 2


def test_list_open_excludes_closed_items():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    refresh_mod_queue(conn, CONFIG, NOW)
    with conn:
        conn.execute("UPDATE mod_queue SET status = 'dismissed'")
    assert list_open(conn) == []
