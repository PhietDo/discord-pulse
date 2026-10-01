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


def test_strip_unknown_normalizes_angle_brackets_and_spaces():
    cleaned, removed = strip_unknown("A [[msg:<m1>]] B [[msg: m2 ]] C [[msg: < m3 > ]].", {"m1", "m2", "m3"})
    assert cleaned == "A [[msg:m1]] B [[msg:m2]] C [[msg:m3]]."
    assert removed == []
    assert cited_ids(cleaned) == ["m1", "m2", "m3"]


def test_strip_unknown_sweeps_remaining_malformed_tokens():
    cleaned, removed = strip_unknown("A [[msg:1]] B [[msg:two words]] C [[msg:]] D [[msg:<x>]].", {"1"})
    assert cleaned == "A [[msg:1]] B  C  D ."
    assert removed == ["x", "two words", ""]  # strict pass first, then the sweep


def test_strip_unknown_dedupes_removed_in_order():
    _, removed = strip_unknown("[[msg:x]] [[msg:y]] [[msg:x]] [[msg:a b]] [[msg:a b]]", set())
    assert removed == ["x", "y", "a b"]
