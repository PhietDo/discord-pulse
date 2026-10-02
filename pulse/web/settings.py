from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from pulse.agents.llm import LLMClient
from pulse.config import Config


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class WebSettings:
    db_path: Path
    config: Config
    server_name: str = "Discord server"
    demo: bool = False
    clock: Callable[[], datetime] = field(default=_utc_now)
    llm_factory: Callable[[sqlite3.Connection, Config], LLMClient] | None = None
    allowed_hosts: tuple[str, ...] | None = None

    @property
    def agents_on(self) -> bool:
        return self.llm_factory is not None and not self.demo

    @property
    def agents_off_text(self) -> str:
        return "Agents are off in demo mode" if self.demo else "Agents are off"
