import json
from dataclasses import replace

from pulse.config import Launch
from pulse.db import connect
from pulse.store import sync_launches, upsert_messages
from tests.fakes import msg, set_triage

TEAM = frozenset({"t1"})


def test_inserts_new_messages_and_marks_team():
    conn = connect(":memory:")
    stats = upsert_messages(conn, [msg("m1"), msg("m2", author_id="t1")], TEAM)
    assert (stats.inserted, stats.updated, stats.unchanged) == (2, 0, 0)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM messages")}
    assert rows["m1"]["is_team"] == 0
    assert rows["m2"]["is_team"] == 1
    assert rows["m1"]["created_at"] == "2026-09-28T12:00:00.000000Z"


def test_reimport_is_idempotent():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1"), msg("m2")], TEAM)
    stats = upsert_messages(conn, [msg("m1"), msg("m2")], TEAM)
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 0, 2)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


def test_edited_content_invalidates_triage():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", "broken")], TEAM)
    set_triage(conn, "m1", sentiment=-1)
    stats = upsert_messages(conn, [msg("m1", "fixed now, thanks")], TEAM)
    assert stats.updated == 1
    assert conn.execute("SELECT content FROM messages WHERE id='m1'").fetchone()[0] == "fixed now, thanks"
    assert conn.execute("SELECT count(*) FROM triage").fetchone()[0] == 0


def test_unchanged_content_keeps_triage_but_refreshes_team_flag():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", author_id="u9")], TEAM)
    set_triage(conn, "m1")
    upsert_messages(conn, [msg("m1", author_id="u9")], frozenset({"u9"}))
    assert conn.execute("SELECT is_team FROM messages WHERE id='m1'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM triage").fetchone()[0] == 1


def test_stores_optional_fields():
    conn = connect(":memory:")
    m = replace(msg("m1", thread_id="300", reply_to_id="m0", is_bot=True), author_avatar_url="https://x/a.png")
    upsert_messages(conn, [m], TEAM)
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert (row["thread_id"], row["reply_to_id"], row["is_bot"], row["author_avatar_url"]) == (
        "300", "m0", 1, "https://x/a.png"
    )


def test_sync_launches_upserts_by_name():
    conn = connect(":memory:")
    assert sync_launches(conn, [Launch("v2.0 SDK", "2026-09-15", ("v2", "migration"))]) == 1
    sync_launches(conn, [Launch("v2.0 SDK", "2026-09-16", ("v2",)), Launch("CLI 3", "2026-10-01", ())])
    rows = {r["name"]: r for r in conn.execute("SELECT * FROM launches")}
    assert set(rows) == {"v2.0 SDK", "CLI 3"}
    assert rows["v2.0 SDK"]["date"] == "2026-09-16"
    assert json.loads(rows["v2.0 SDK"]["keywords"]) == ["v2"]
