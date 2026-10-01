"""Jinja filters for the dashboard."""
from __future__ import annotations

from datetime import datetime

from pulse.models import from_iso

KIND_LABELS = {
    "bug": "bug", "question": "question", "feature_request": "feature request",
    "docs": "docs", "praise": "praise", "other": "other",
}
_PALETTE = ("#0E7C86", "#7C3AED", "#B45309", "#2563EB", "#BE185D", "#15803D", "#9333EA", "#C2410C", "#0369A1", "#4D7C0F")


def age(created_at: str, now: datetime) -> str:
    hours = (now - from_iso(created_at)).total_seconds() / 3600
    if hours < 1:
        return f"{max(1, round(hours * 60))} min ago"
    if hours < 48:
        return f"{round(hours)}h ago"
    return f"{round(hours / 24)}d ago"


def utc(created_at: str) -> str:
    dt = from_iso(created_at)
    return f"{dt:%b} {dt.day}, {dt:%H:%M} UTC"


def signed(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    if value == 0:
        return "0" if digits == 0 else f"{0:.{digits}f}"
    return f"{value:+.{digits}f}"


def minutes(value) -> str:
    if value is None:
        return "—"
    if value < 60:
        return f"{round(value)} min"
    if value < 48 * 60:
        return f"{value / 60:.1f} h"
    return f"{value / 1440:.1f} d"


def avatar_color(name: str) -> str:
    return _PALETTE[sum(map(ord, name)) % len(_PALETTE)]


def kind_label(kind: str | None) -> str:
    return KIND_LABELS.get(kind or "", kind or "")
