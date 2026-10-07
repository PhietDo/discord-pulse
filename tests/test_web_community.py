from dataclasses import replace

from pulse.db import connect
from pulse.web.charts import heatmap
from tests.web_fakes import CONFIG, make_client


def test_heatmap_svg():
    grid = [[0] * 24 for _ in range(7)]
    grid[1][9] = 4
    grid[1][10] = 2
    svg = str(heatmap(grid, "Questions by hour"))
    assert svg.startswith('<svg class="heat"') and 'aria-label="Questions by hour"' in svg
    assert "<title>Tue 09:00 · 4</title>" in svg and 'fill-opacity="1.00"' in svg
    assert svg.count("<rect") == 7 * 24
    assert "fill-opacity" not in str(heatmap([[0] * 24 for _ in range(7)], "empty"))


def test_community_page_renders_sections(tmp_path):
    client = make_client(tmp_path)
    conn = connect(tmp_path / "pulse.db")
    with conn:
        conn.execute("INSERT INTO reactions (message_id, emoji, count) VALUES ('p1', '🎉', 6)")
    conn.close()
    html = client.get("/community?days=7").text
    for heading in ("When questions arrive", "Coverage gaps", "Newcomers", "Community helpers", "Most reacted"):
        assert heading in html
    assert '<svg class="heat"' in html and "🎉 6" in html
    assert 'href="/community' in client.get("/").text


def test_community_respects_channel_filter(tmp_path):
    html = make_client(tmp_path).get("/community?days=7&channel=999").text
    assert "No feature requests with reactions" in html


def test_community_uses_configured_timezone(tmp_path):
    html = make_client(tmp_path, config=replace(CONFIG, timezone="Asia/Tokyo")).get("/community").text
    assert "Asia/Tokyo" in html


def test_community_empty_db(tmp_path):
    r = make_client(tmp_path, seeded=False).get("/community")
    assert r.status_code == 200 and "No newcomers" in r.text
