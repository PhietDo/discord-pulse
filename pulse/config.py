"""Load and validate pulse.toml."""
from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pulse.models import KINDS

PROVIDERS = ("anthropic", "openai", "openrouter")
CLASSIFIER_PROVIDERS = ("jev",)
AGENTS = ("triage", "theme", "digest", "investigate")
KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "jev": "OPENROUTER_API_KEY",
}
DEFAULT_ESCALATE_KINDS = ("bug", "docs", "feature_request", "praise")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class ModelRef:
    provider: str
    model: str

    @classmethod
    def parse(cls, value: str, providers: tuple[str, ...] = PROVIDERS) -> ModelRef:
        provider, sep, model = value.partition(":")
        if not sep or not model or provider not in providers:
            raise ConfigError(
                f"model must be 'provider:model' with provider in {providers}, got {value!r}"
            )
        return cls(provider, model)

    def __str__(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass(frozen=True)
class Price:
    """USD per million tokens, or per request for request-priced models (Jev)."""

    input: float
    output: float
    cache_read: float = 0.0
    per_request: float = 0.0


@dataclass(frozen=True)
class Launch:
    name: str
    date: str
    keywords: tuple[str, ...]


@dataclass(frozen=True)
class ClassifierConfig:
    enabled: bool
    model: ModelRef
    needs_reply_threshold: float = 0.7
    min_confidence: float = 0.6
    escalate_kinds: tuple[str, ...] = DEFAULT_ESCALATE_KINDS


@dataclass(frozen=True)
class IntegrationsConfig:
    github_repo: str | None = None
    github_labels: tuple[str, ...] = ()
    linear_team_id: str | None = None


@dataclass(frozen=True)
class AlertsConfig:
    enabled: bool = False
    spike_min_volume: int = 5
    spike_trend: float = 1.0
    frustrated_hours: float = 12.0


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
    classifier: ClassifierConfig | None = None
    bot_backfill_days: int = 30
    integrations: IntegrationsConfig = IntegrationsConfig()
    alerts: AlertsConfig = AlertsConfig()
    timezone: str = "UTC"


def _price(name: str, raw: Any) -> Price:
    try:
        if "per_request" in raw:
            return Price(input=0.0, output=0.0, per_request=float(raw["per_request"]))
        return Price(
            input=float(raw["input"]),
            output=float(raw["output"]),
            cache_read=float(raw.get("cache_read", 0.0)),
        )
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f'[pricing."{name}"] needs numeric input and output (or per_request): {e}') from e


def _launch(raw: Any) -> Launch:
    name = raw.get("name") if isinstance(raw, dict) else None
    try:
        date.fromisoformat(raw["date"])
        return Launch(str(raw["name"]), raw["date"], tuple(str(k) for k in raw.get("keywords", [])))
    except (KeyError, TypeError, ValueError) as e:
        raise ConfigError(f"launch {name!r}: needs a name and a YYYY-MM-DD date") from e


def _unit_interval(name: str, value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"[classifier] {name} must be a number between 0 and 1") from e
    if not 0.0 <= v <= 1.0:
        raise ConfigError(f"[classifier] {name} must be between 0 and 1, got {v}")
    return v


def _classifier(raw: Any, env: Mapping[str, str], require_keys: bool = True) -> ClassifierConfig | None:
    if not raw:
        return None
    model = ModelRef.parse(str(raw.get("model", "jev:jev-latest")), CLASSIFIER_PROVIDERS)
    kinds = tuple(str(k) for k in raw.get("escalate_kinds", DEFAULT_ESCALATE_KINDS))
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ConfigError(f"[classifier] escalate_kinds has unknown kinds {unknown}; allowed {list(KINDS)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"[classifier] enabled must be a boolean, got {enabled!r}")
    cfg = ClassifierConfig(
        enabled=enabled,
        model=model,
        needs_reply_threshold=_unit_interval("needs_reply_threshold", raw.get("needs_reply_threshold", 0.7)),
        min_confidence=_unit_interval("min_confidence", raw.get("min_confidence", 0.6)),
        escalate_kinds=kinds,
    )
    if require_keys and cfg.enabled and not env.get(KEY_ENV[model.provider]):
        raise ConfigError(f"{KEY_ENV[model.provider]} must be set because the classifier {model} is enabled")
    return cfg


def _valid_repo(repo: str) -> bool:
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return False
    owner, _, name = repo.partition("/")
    return owner not in (".", "..") and name not in (".", "..")


def _table(raw: Any, name: str) -> dict:
    """A [section] that may be absent; present but not a table is a config error."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"[{name}] must be a table (a [{name}] section), got {raw!r}")
    return raw


def _integrations(raw: Any) -> IntegrationsConfig:
    raw = _table(raw, "integrations")
    github = _table(raw.get("github"), "integrations.github")
    linear = _table(raw.get("linear"), "integrations.linear")
    repo = github.get("repo")
    if repo is not None and not _valid_repo(str(repo)):
        raise ConfigError(f"[integrations.github] repo must look like owner/name, got {repo!r}")
    labels = github.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise ConfigError("[integrations.github] labels must be a list of strings")
    team = linear.get("team_id")
    if team is not None and not isinstance(team, str):
        raise ConfigError(f"[integrations.linear] team_id must be a string, got {team!r}")
    return IntegrationsConfig(
        github_repo=str(repo) if repo else None,
        github_labels=tuple(labels),
        linear_team_id=str(team) if team else None,
    )


def _alert_number(raw: dict, name: str, default: float) -> float:
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[alerts] {name} must be a number, not true/false or text, got {value!r}")
    if not math.isfinite(value):
        raise ConfigError(f"[alerts] {name} must be a finite number, got {value!r}")
    return float(value)


def _alerts(raw: Any) -> AlertsConfig:
    raw = _table(raw, "alerts")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"[alerts] enabled must be a boolean, got {enabled!r}")
    volume = raw.get("spike_min_volume", 5)
    if isinstance(volume, bool) or not isinstance(volume, int):
        raise ConfigError(f"[alerts] spike_min_volume must be a whole number, not true/false or text, got {volume!r}")
    cfg = AlertsConfig(
        enabled=enabled,
        spike_min_volume=volume,
        spike_trend=_alert_number(raw, "spike_trend", 1.0),
        frustrated_hours=_alert_number(raw, "frustrated_hours", 12.0),
    )
    if cfg.spike_min_volume < 1 or cfg.spike_trend < 0 or cfg.frustrated_hours <= 0:
        raise ConfigError("[alerts] spike_min_volume must be at least 1 and the other values positive")
    return cfg


def load_config(path: str | Path, env: Mapping[str, str] | None = None, *, require_keys: bool = True) -> Config:
    path = Path(path)
    env = os.environ if env is None else env
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError as e:
        raise ConfigError(f"cannot read {path}: {e} (copy pulse.toml.example to pulse.toml)") from e
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"cannot read {path}: {e}") from e

    server = raw.get("server", {})
    guild_id = str(server.get("guild_id", "")).strip()
    if not guild_id:
        raise ConfigError("[server] guild_id is required")

    tz = str(server.get("timezone", "UTC"))
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise ConfigError(f'[server] timezone {tz!r} is not a known time zone (for example "America/New_York")') from e

    models_raw = raw.get("models", {})
    models: dict[str, ModelRef] = {}
    for agent in AGENTS:
        if agent not in models_raw:
            raise ConfigError(f"[models] {agent} is required")
        models[agent] = ModelRef.parse(str(models_raw[agent]))

    pricing = {name: _price(name, p) for name, p in raw.get("pricing", {}).items()}

    for ref in models.values():
        var = KEY_ENV[ref.provider]
        if require_keys and not env.get(var):
            raise ConfigError(f"{var} must be set because {ref} is configured")
        if ref.provider != "openrouter" and str(ref) not in pricing:
            raise ConfigError(f'[pricing."{ref}"] is required so the budget cap can be enforced')

    mod_queue = raw.get("mod_queue", {})
    paths = raw.get("paths", {})
    base = path.parent

    backfill_days = _table(raw.get("bot"), "bot").get("backfill_days", 30)
    if isinstance(backfill_days, bool) or not isinstance(backfill_days, int) or backfill_days < 1:
        raise ConfigError(f"[bot] backfill_days must be a whole number of days, at least 1, got {backfill_days!r}")

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
        classifier=_classifier(raw.get("classifier"), env, require_keys),
        bot_backfill_days=backfill_days,
        integrations=_integrations(raw.get("integrations")),
        alerts=_alerts(raw.get("alerts")),
        timezone=tz,
    )


def missing_keys(config: Config, env: Mapping[str, str] | None = None) -> list[str]:
    """Env vars the configured models (and an enabled classifier) need but that are unset."""
    env = os.environ if env is None else env
    needed = {KEY_ENV[r.provider] for r in config.models.values()}
    if config.classifier is not None and config.classifier.enabled:
        needed.add(KEY_ENV[config.classifier.model.provider])
    return sorted(v for v in needed if not env.get(v))
