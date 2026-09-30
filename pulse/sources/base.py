"""The one ingest interface. File import and the bot adapter both implement it."""
from __future__ import annotations

from datetime import datetime
from typing import Iterator, Protocol

from pulse.models import Message


class Source(Protocol):
    errors: list[str]

    def fetch(self, since: datetime | None = None) -> Iterator[Message]: ...
