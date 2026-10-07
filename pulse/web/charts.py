"""Server-side SVG charts in the mockup's style (spec 15.5)."""
from __future__ import annotations

from datetime import date
from html import escape

from markupsafe import Markup

from pulse.community import WEEKDAYS

_W, _H, _L, _R, _T, _B = 640, 250, 44, 14, 16, 30


def _nice(value: int) -> int:
    for top in (10, 20, 40, 50, 80, 100, 200, 400, 500, 800, 1000, 2000, 5000, 10000):
        if value <= top:
            return top
    return -(-value // 10000) * 10000


def _short_day(day: str) -> str:
    d = date.fromisoformat(day)
    return f"{d:%b} {d.day}"


def sentiment_chart(series: list[dict], markers: list[tuple[str, str]]) -> Markup:
    """Daily average sentiment (line, clamped to -1..+1) over message volume (bars), with
    vertical markers at the start of each marker day that falls inside the series."""
    if not any(p["messages"] for p in series):
        return Markup('<p class="empty">No messages in this window.</p>')
    n = len(series)
    iw, ih = _W - _L - _R, _H - _T - _B

    def x(i: int) -> float:
        return _L + iw * (i + 0.5) / n

    def ys(v: float) -> float:
        return _T + ih * (1 - (max(-1.0, min(1.0, v)) + 1) / 2)

    top = _nice(max(p["messages"] for p in series))

    def yn(c: float) -> float:
        return _T + ih * (1 - c / top)

    parts: list[str] = []
    for v in (1.0, 0.5, 0.0, -0.5, -1.0):
        y = ys(v)
        parts.append(f'<line class="{"zero" if v == 0 else "grid"}" x1="{_L}" x2="{_W - _R}" y1="{y:.1f}" y2="{y:.1f}"/>')
        label = "0" if v == 0 else f"{v:+g}"
        parts.append(f'<text x="{_L - 8}" y="{y + 4:.1f}" text-anchor="end">{label}</text>')
    for c in (top // 2, top):
        parts.append(f'<text x="{_W - _R}" y="{yn(c) - 4:.1f}" text-anchor="end">{c} msgs</text>')
    bw = iw / n * 0.56
    for i, p in enumerate(series):
        if p["messages"]:
            y = yn(p["messages"])
            parts.append(
                f'<rect class="bar" x="{x(i) - bw / 2:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{_T + ih - y:.1f}" rx="2">'
                f'<title>{p["day"]}: {p["messages"]} messages</title></rect>'
            )
    step = max(1, round(n / 7))
    for i, p in enumerate(series):
        if (n - 1 - i) % step == 0:
            parts.append(f'<text x="{x(i):.1f}" y="{_H - 10}" text-anchor="middle">{_short_day(p["day"])}</text>')
    days = [p["day"] for p in series]
    for day, label in markers:
        if day in days:
            lx = x(days.index(day)) - iw / n * 0.5
            parts.append(f'<line class="launch" x1="{lx:.1f}" x2="{lx:.1f}" y1="{_T}" y2="{_T + ih}"/>')
            parts.append(f'<text class="launch-t" x="{lx - 6:.1f}" y="{_T + ih - 8}" text-anchor="end">{escape(label)}</text>')
    points = [(x(i), ys(p["avg_sentiment"]), p["avg_sentiment"]) for i, p in enumerate(series) if p["avg_sentiment"] is not None]
    if points:
        d = " ".join(f'{"M" if j == 0 else "L"}{px:.1f} {py:.1f}' for j, (px, py, _) in enumerate(points))
        parts.append(f'<path class="area" d="{d} L{points[-1][0]:.1f} {ys(-1):.1f} L{points[0][0]:.1f} {ys(-1):.1f} Z"/>')
        parts.append(f'<path class="line" d="{d}"/>')
        for px, py, _ in points[:-1]:
            parts.append(f'<circle class="dot" cx="{px:.1f}" cy="{py:.1f}" r="2.4"/>')
        px, py, last = points[-1]
        parts.append(f'<circle class="end" cx="{px:.1f}" cy="{py:.1f}" r="4.5"/>')
        parts.append(f'<text class="end-t" x="{px - 8:.1f}" y="{py + 18:.1f}" text-anchor="end">{last:+.2f}</text>')
    return Markup(
        f'<svg class="chart" viewBox="0 0 {_W} {_H}" role="img" aria-label="Daily average sentiment and message volume">'
        + "".join(parts) + "</svg>"
    )


def sparkline(values: list[int]) -> Markup:
    w, h = 92, 28
    vals = values or [0]
    top = max(max(vals), 1)
    count = max(len(vals) - 1, 1)

    def x(i: int) -> float:
        return 2 + (w - 4) * i / count

    def y(v: int) -> float:
        return h - 3 - (h - 6) * v / top

    d = " ".join(f'{"M" if i == 0 else "L"}{x(i):.1f} {y(v):.1f}' for i, v in enumerate(vals))
    last = len(vals) - 1
    return Markup(
        f'<svg class="spark" viewBox="0 0 {w} {h}" aria-hidden="true">'
        f'<path class="a" d="{d} L{x(last):.1f} {h} L{x(0):.1f} {h} Z"/><path class="l" d="{d}"/>'
        f'<circle cx="{x(last):.1f}" cy="{y(vals[-1]):.1f}" r="2.4"/></svg>'
    )


def heatmap(grid: list[list[int]], label: str) -> Markup:
    """7 x 24 grid (Mon..Sun x hour) as SVG cells shaded relative to the busiest cell."""
    cw, ch, left, top = 22, 18, 34, 18
    w, h = left + 24 * cw, top + 7 * ch
    peak = max((max(row) for row in grid), default=0)
    parts = [f'<svg class="heat" viewBox="0 0 {w} {h}" role="img" aria-label="{escape(label)}">']
    for hour in range(0, 24, 3):
        parts.append(f'<text class="ax" x="{left + hour * cw + cw / 2:.0f}" y="12" text-anchor="middle">{hour:02d}</text>')
    for d, row in enumerate(grid):
        y = top + d * ch
        parts.append(f'<text class="ax" x="{left - 6}" y="{y + ch - 5}" text-anchor="end">{WEEKDAYS[d]}</text>')
        for hour, v in enumerate(row):
            shade = f' fill-opacity="{0.15 + 0.85 * v / peak:.2f}"' if v and peak else ""
            parts.append(
                f'<rect class="{"hm" if v else "hm0"}" x="{left + hour * cw + 1}" y="{y + 1}" width="{cw - 2}"'
                f' height="{ch - 2}" rx="2"{shade}><title>{WEEKDAYS[d]} {hour:02d}:00 · {v}</title></rect>'
            )
    parts.append("</svg>")
    return Markup("".join(parts))
