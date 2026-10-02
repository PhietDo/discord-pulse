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


def _html_conn(rows):
    conn = connect(":memory:")
    upsert_messages(conn, rows, frozenset())
    return conn


def _hrefs(out: str) -> list[str]:
    import re

    return re.findall(r'href="([^"]*)"', out)


def test_render_html_cited_content_cannot_inject_attributes():
    from pulse.citations import render_html

    conn = _html_conn([msg("m1", "hi [y](x/onmouseover=alert(1)//)", author_name="alice")])
    out = render_html("See [[msg:m1]] here", conn)
    import html.parser

    attrs: list = []

    class P(html.parser.HTMLParser):
        def handle_starttag(self, tag, a):
            attrs.extend(a)

    P().feed(out)
    assert all(name != "onmouseover" for name, _ in attrs)
    assert 'class="cite"' in out and "@alice" in out


def test_render_html_cited_author_name_is_not_a_link():
    from pulse.citations import render_html

    conn = _html_conn([msg("m1", "hi", author_name="[x](javascript:alert(1))")])
    out = render_html("By [[msg:m1]]", conn)
    assert _hrefs(out) == ["https://discord.com/channels/900/100/m1"]
    assert "@[x](javascript:alert(1))</a>" in out


def test_render_html_drops_unsafe_link_schemes_and_images():
    from pulse.citations import render_html

    conn = _html_conn([])
    md = (
        "[a](javascript:alert(1)) [b](JAVASCRIPT:alert(1)) [c](data:text/html,x) [e]( java\tscript:alert(1))\n\n"
        "[d][1]\n\n![i](https://x/y.png)\n\n[docs](https://example.com) [m](mailto:a@b.c) [r](/pain) [h](#top)\n\n"
        "[1]: javascript:alert(1)\n"
    )
    out = render_html(md, conn)
    hrefs = _hrefs(out)
    assert hrefs == ["https://example.com", "mailto:a@b.c", "/pain", "#top"]
    assert "<img" not in out
    assert "javascript" not in out.lower() and "data:" not in out
    assert '<a href="https://example.com">docs</a>' in out


def test_protocol_relative_links_are_dropped():
    from pulse.citations import safe_href

    assert not safe_href("//evil.com/x") and not safe_href("/\\evil.com") and not safe_href(" //evil.com")
    assert safe_href("/pain?theme=1") and safe_href("#top") and safe_href("https://example.com")
