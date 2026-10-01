"""Core data types and timestamp helpers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

KINDS = ("bug", "question", "feature_request", "docs", "praise", "other")

_ISO_FMT = "%Y-%m-%dT%H:%M:%S.%fZ"


def to_iso(dt: datetime) -> str:
    """Fixed-width UTC string, so stored timestamps sort lexically."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime; timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime(_ISO_FMT)


def from_iso(value: str) -> datetime:
    return datetime.strptime(value, _ISO_FMT).replace(tzinfo=timezone.utc)


def parse_timestamp(value: str) -> datetime:
    """Parse an ISO-8601 timestamp from an export. Naive values are treated as UTC."""
    dt = datetime.fromisoformat(value.strip())
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class Message:
    id: str
    guild_id: str
    channel_id: str
    author_id: str
    author_name: str
    content: str
    created_at: datetime
    channel_name: str = ""
    thread_id: str | None = None
    author_avatar_url: str | None = None
    is_bot: bool = False
    is_team: bool = False
    edited_at: datetime | None = None
    reply_to_id: str | None = None
    source: str = "file"
    parent_channel_id: str | None = None


@dataclass(frozen=True)
class TriageResult:
    message_id: str
    sentiment: int
    confidence: float
    kind: str
    topics: tuple[str, ...]
    needs_reply: bool
