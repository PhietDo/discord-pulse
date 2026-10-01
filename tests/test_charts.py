from pulse.web.charts import sentiment_chart, sparkline


def _series(*points):
    return [{"day": f"2026-09-{d:02d}", "messages": n, "avg_sentiment": s} for d, n, s in points]


def test_chart_empty_window_shows_message():
    html = str(sentiment_chart(_series((28, 0, None), (29, 0, None)), []))
    assert "No messages in this window" in html and "<svg" not in html


def test_chart_draws_bars_line_and_launch_marker():
    html = str(sentiment_chart(_series((27, 10, 0.4), (28, 60, -0.2), (29, 50, -0.5)), [("2026-09-28", "v2.0")]))
    assert html.startswith("<svg") and html.count('class="bar"') == 3
    assert 'class="line"' in html and "v2.0" in html and "-0.50" in html


def test_chart_skips_marker_outside_window_and_escapes_label():
    html = str(sentiment_chart(_series((27, 3, 0.1)), [("2026-08-01", "old"), ("2026-09-27", "<b>x</b>")]))
    assert "old" not in html and "&lt;b&gt;x&lt;/b&gt;" in html


def test_chart_clamps_sentiment_to_axis():
    html = str(sentiment_chart(_series((27, 3, -2.0), (28, 3, -1.0)), []))
    # both points sit on the -1 gridline (y = 16 + 204 = 220.0)
    assert 'cy="220.0"' in html


def test_sparkline_handles_zeros():
    html = str(sparkline([0, 0, 0]))
    assert html.startswith('<svg class="spark"') and 'class="l"' in html
