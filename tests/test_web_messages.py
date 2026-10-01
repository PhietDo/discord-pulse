from tests.web_fakes import make_client


def _ids(html):
    return {mid for mid in ("q1", "q2", "s1", "q3", "p1", "g1", "g2") if f'id="msg-{mid}"' in html}


def test_messages_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/messages")
    assert r.status_code == 200 and "No messages match" in r.text


def test_messages_lists_everyone_newest_first(tmp_path):
    html = make_client(tmp_path).get("/messages").text
    assert _ids(html) == {"q1", "q2", "s1", "q3", "p1", "g1", "g2"}
    assert html.index('id="msg-g2"') < html.index('id="msg-q1"')
    assert "7 messages" in html


def test_messages_by_author(tmp_path):
    html = make_client(tmp_path).get("/messages?author_id=u1").text
    assert _ids(html) == {"q1"} and "Messages from alice" in html


def test_messages_text_kind_theme_and_mood_filters(tmp_path):
    client = make_client(tmp_path)
    assert _ids(client.get("/messages?q=wheel").text) == {"q3"}
    assert _ids(client.get("/messages?q=%25").text) == set()  # a literal %, not a wildcard
    assert _ids(client.get("/messages?kind=bug").text) == {"q1", "q3", "g2"}
    assert _ids(client.get("/messages?theme=1").text) == {"q1", "q3"}
    assert _ids(client.get("/messages?mood=pos").text) == {"p1"}


def test_messages_bad_params(tmp_path):
    r = make_client(tmp_path).get("/messages?page=-3&theme=abc&kind=zzz&mood=x")
    assert r.status_code == 200 and len(_ids(r.text)) == 7


def test_messages_second_page_is_empty_with_previous_link(tmp_path):
    html = make_client(tmp_path).get("/messages?page=2").text
    assert "No messages match" in html and "Previous" in html


def test_messages_huge_page_is_empty(tmp_path):
    r = make_client(tmp_path).get("/messages?page=" + "9" * 30)
    assert r.status_code == 200 and "No messages match" in r.text
