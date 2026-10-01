from pulse.citations import cited_ids, render_text, strip_unknown
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import msg


def test_cited_ids_unique_in_order():
    md = "A [[msg:2]] then [[msg:1]] and again [[msg:2]]."
    assert cited_ids(md) == ["2", "1"]


def test_strip_unknown_removes_and_reports():
    cleaned, removed = strip_unknown("Good [[msg:1]] bad [[msg:x9]] ok.", {"1"})
    assert cleaned == "Good [[msg:1]] bad  ok."
    assert removed == ["x9"]


def test_render_text_links_known_and_marks_missing():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", "hi", author_name="alice")], frozenset())
    text = render_text("See [[msg:m1]] and [[msg:gone]].", conn)
    assert text == "See (alice, https://discord.com/channels/900/100/m1) and [missing message]."
