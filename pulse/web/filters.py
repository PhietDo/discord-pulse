"""The window and channel filter shared by every view (spec 15.3)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlencode

WINDOWS = (7, 14, 30, 90)
DEFAULT_DAYS = 14


@dataclass(frozen=True)
class Filters:
    days: int
    channel: str | None
    now: datetime

    @property
    def start(self) -> datetime:
        return self.now - timedelta(days=self.days)

    @property
    def end(self) -> datetime:
        return self.now

    @property
    def channels(self) -> tuple[str, ...] | None:
        return (self.channel,) if self.channel else None

    def qs(self, **extra) -> str:
        """Query string keeping the filters; extra keys are added or override, empty ones are dropped."""
        params: dict = {"days": self.days}
        if self.channel:
            params["channel"] = self.channel
        params.update(extra)
        return "?" + urlencode({k: v for k, v in params.items() if v not in (None, "")})


def parse_filters(days: str | None, channel: str | None, now: datetime, known_channels: set[str]) -> Filters:
    try:
        d = int(days) if days else DEFAULT_DAYS
    except ValueError:
        d = DEFAULT_DAYS
    if d not in WINDOWS:
        d = DEFAULT_DAYS
    return Filters(d, channel if channel and channel in known_channels else None, now)
