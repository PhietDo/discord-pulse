from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pulse.community import activity_heatmap, coverage_gaps, helpers, most_wanted, newcomers, top_reacted
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import T0, msg, set_triage

START, END = T0 - timedelta(days=1), T0 + timedelta(days=3)
TEAM = frozenset({"t1"})


def m(id, *, minutes=0, author="u1", channel="100", thread=None, reply_to=None, bot=False, reactions=()):
    base = msg(id, f"text {id}", minutes=minutes, channel_id=channel, thread_id=thread,
               author_id=author, author_name=author, reply_to_id=reply_to, is_bot=bot)
    return replace(base, reactions=reactions)


def db(messages, labels):
    conn = connect(":memory:")
    upsert_messages(conn, messages, TEAM)
    for mid, kw in labels.items():
        set_triage(conn, mid, **kw)
    return conn


def total(grid):
    return sum(map(sum, grid))


def test_heatmap_counts_questions_and_staff_in_the_configured_zone():
    conn = db(
        [m("q"), m("s", minutes=60, author="t1"), m("c", minutes=5, author="u2"),
         m("b", minutes=6, author="bot", bot=True), m("other", minutes=7, author="u3", channel="200")],
        {"q": dict(needs_reply=True), "c": dict(needs_reply=False), "b": dict(needs_reply=True),
         "other": dict(needs_reply=True)},
    )
    utc = activity_heatmap(conn, START, END)
    assert utc["questions"][T0.weekday()][T0.hour] == 2
    assert utc["staff"][T0.weekday()][T0.hour + 1] == 1
    assert total(utc["questions"]) == 2 and total(utc["staff"]) == 1
    ny = activity_heatmap(conn, START, END, "America/New_York")
    local = T0.astimezone(ZoneInfo("America/New_York"))
    assert ny["questions"][local.weekday()][local.hour] == 2 and ny["timezone"] == "America/New_York"
    assert total(activity_heatmap(conn, START, END, channels=("100",))["questions"]) == 1


def test_coverage_gaps_prefers_unstaffed_busy_hours():
    q = [[0] * 24 for _ in range(7)]
    s = [[0] * 24 for _ in range(7)]
    q[0][9], s[0][9] = 5, 4
    q[1][2] = 3
    q[2][14] = 1
    q[3][3] = 6
    q[6][23] = 1
    s[6][23] = 9
    gaps = coverage_gaps({"questions": q, "staff": s})
    assert [g["label"] for g in gaps] == ["Thu 03:00–04:00", "Tue 02:00–03:00", "Wed 14:00–15:00"]
    assert gaps[0] == {"label": "Thu 03:00–04:00", "questions": 6, "staff": 0}
    assert coverage_gaps({"questions": [[0] * 24 for _ in range(7)], "staff": s}) == []
    assert coverage_gaps({"questions": q, "staff": s}, top=5)[-1]["label"] == "Sun 23:00–00:00"


def test_newcomers_reply_and_return():
    msgs = [
        m("old", minutes=-10 * 24 * 60, author="u3"), m("u3b", minutes=30, author="u3"),
        m("n1", author="u1"), m("r1", minutes=30, author="t1", reply_to="n1"),
        m("n2", minutes=120, author="u2"), m("n2b", minutes=120 + 2 * 24 * 60, author="u2"),
        m("n4", minutes=200, author="u4", thread="300"), m("r4", minutes=230, author="u5", thread="300"),
        m("n6", minutes=300, author="u6"), m("r6", minutes=310, author="u7", thread="n6"),
        m("n8", minutes=400, author="u8"), m("own", minutes=410, author="u8", reply_to="n8"),
        m("late", minutes=500, author="u9"), m("lr", minutes=500 + 3 * 24 * 60, author="t1", reply_to="late"),
        m("botnew", minutes=600, author="b", bot=True),
    ]
    nc = newcomers(db(msgs, {}), START, END)
    assert nc["count"] == 8
    assert nc["replied"] == 3
    assert nc["returned"] == 1
    assert nc["unreplied_ids"] == ["late", "n8", "r6", "r4", "n2"]
    assert sum(w["count"] for w in nc["weekly"]) == 8
    assert all(date.fromisoformat(w["week"]).weekday() == 0 for w in nc["weekly"])
    assert newcomers(db(msgs, {}), START, END, channels=("200",))["count"] == 0


def test_newcomers_weekly_buckets_use_the_configured_timezone():
    at = datetime(2026, 3, 2, 0, 30, tzinfo=timezone.utc)
    conn = db([replace(m("n1", author="u1"), created_at=at)], {})
    nc = newcomers(conn, at, at + timedelta(minutes=1), tz="America/New_York")
    assert nc["weekly"] == [{"week": "2026-02-23", "count": 1}]


def test_helpers_rank_members_who_answer_others():
    msgs = [
        m("q1", author="u1"), m("a1", minutes=10, author="u2", reply_to="q1"),
        m("q2", minutes=20, author="u3"), m("a2", minutes=30, author="u2", reply_to="q2"),
        m("q3", minutes=40, author="u4", thread="300"), m("a3", minutes=50, author="u5", thread="300"),
        m("self", minutes=60, author="u1", reply_to="q1"),
        m("staff", minutes=70, author="t1", reply_to="q2"),
        m("chat", minutes=80, author="u6"), m("a4", minutes=90, author="u7", reply_to="chat"),
        m("botq", minutes=95, author="u8"), m("bota", minutes=96, author="bot", bot=True, reply_to="botq"),
    ]
    labels = {"q1": dict(needs_reply=True), "q2": dict(needs_reply=True), "q3": dict(needs_reply=True),
              "chat": dict(needs_reply=False), "botq": dict(needs_reply=True)}
    board = helpers(db(msgs, labels), START, END)
    assert [(h["author_id"], h["answers"], h["helped"]) for h in board] == [("u2", 2, 2), ("u5", 1, 1)]
    assert board[0]["sample_id"] == "a2" and board[0]["author_name"] == "u2"
    assert helpers(db(msgs, labels), START, END, channels=("999",)) == []


def test_helpers_skips_answers_that_themselves_need_a_reply():
    msgs = [m("q1", author="u_a", thread="900"), m("q2", minutes=10, author="u_b", thread="900")]
    labels = {"q1": dict(needs_reply=True), "q2": dict(needs_reply=True)}
    assert helpers(db(msgs, labels), START, END) == []


def test_most_wanted_and_top_reacted():
    msgs = [
        m("f1", author="u1", reactions=(("👍", 9), ("❤️", 2))),
        m("f2", minutes=5, author="u2", reactions=(("👍", 3),)),
        m("f3", minutes=6, author="u3"),
        m("p1", minutes=7, author="u4", reactions=(("🎉", 20),)),
        m("bot", minutes=8, author="b", bot=True, reactions=(("👍", 50),)),
    ]
    labels = {"f1": dict(kind="feature_request"), "f2": dict(kind="feature_request"),
              "f3": dict(kind="feature_request"), "p1": dict(kind="praise")}
    conn = db(msgs, labels)
    wanted = most_wanted(conn, START, END)
    assert [(w["message_id"], w["total"]) for w in wanted] == [("f1", 11), ("f2", 3)]
    assert wanted[0]["emojis"] == [("👍", 9), ("❤️", 2)]
    assert [t["message_id"] for t in top_reacted(conn, START, END)] == ["p1", "f1", "f2"]
    assert most_wanted(conn, START, END, channels=("200",)) == []
