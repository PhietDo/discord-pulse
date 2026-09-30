"""Load and validate pulse.toml."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

PROVIDERS = ("anthropic", "openai", "openrouter")
AGENTS = ("triage", "theme", "digest", "investigate")
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model: str

    @classmethod
    def parse(cls, value: str) -> ModelRef:
        provider, sep, model = value.partition(":")
        if not sep or not model or provider not in PROVIDERS:
            raise ConfigError(
                f"model must be 'provider:model' with provider in {PROVIDERS}, got {value!r}"
            )
        return cls(provider, model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float = 0.0


@dataclass(frozen=True)
class Launch:
    name: str
    date: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    guild_id: str
    channel_ids: tuple[str, ...]
    team_member_ids: frozenset[str]
    reply_window_hours: float
    frustration_threshold: int
    models: dict[str, ModelRef]
    daily_usd_cap: float
    pricing: dict[str, Price]
    launches: tuple[Launch, ...]
    db_path: Path
    imports_dir: Path


def _price(name: str, raw: Any) -> Price:
    try:
        return Price(
            input=float(raw["input"]),
            output=float(raw["output"]),
            cache_read=float(raw.get("cache_read", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f'[pricing."{name}"] needs numeric input and output: {e}') from e


def _launch(raw: Any) -> Launch:
    name = raw.get("name") if isinstance(raw, dict) else None
    try:
        date.fromisoformat(raw["date"])
        return Launch(str(raw["name"]), raw["date"], tuple(str(k) for k in raw.get("keywords", [])))
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"launch {name!r}: needs a name and a YYYY-MM-DD date") from e


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config:
    path = Path(path)
    env = os.environ if env is None else env
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e

    server = raw.get("server", {})
    guild_id = str(server.get("guild_id", "")).strip()
    if not guild_id:
        raise ConfigError("[server] guild_id is required")

    models_raw = raw.get("models", {})
    models: dict[str, ModelRef] = {}
    for agent in AGENTS:
        if agent not in models_raw:
            raise ConfigError(f"[models] {agent} is required")
        models[agent] = ModelRef.parse(str(models_raw[agent]))

    pricing = {name: _price(name, p) for name, p in raw.get("pricing", {}).items()}

    for ref in models.values():
        var = KEY_ENV[ref.provider]
        if not env.get(var):
            raise ConfigError(f"{var} must be set because {ref} is configured")
        if ref.provider != "openrouter" and str(ref) not in pricing:
            raise ConfigError(f'[pricing."{ref}"] is required so the budget cap can be enforced')

    mod_queue = raw.get("mod_queue", {})
    paths = raw.get("paths", {})
    base = path.parent
    return Config(
        guild_id=guild_id,
        channel_ids=tuple(str(c) for c in server.get("channel_ids", [])),
        team_member_ids=frozenset(str(t) for t in server.get("team_member_ids", [])),
        reply_window_hours=float(mod_queue.get("reply_window_hours", 12)),
        frustration_threshold=int(mod_queue.get("frustration_threshold", -2)),
        models=models,
        daily_usd_cap=float(raw.get("budget", {}).get("daily_usd_cap", 5.0)),
        pricing=pricing,
        launches=tuple(_launch(l) for l in raw.get("launches", [])),
        db_path=base / paths.get("db", "pulse.db"),
        imports_dir=base / paths.get("imports", "imports"),
    )
