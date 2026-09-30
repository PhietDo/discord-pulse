# Discord Pulse Plan 1: Core Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Import Discord export files into SQLite, triage every message with a provider-switchable LLM subagent (Anthropic, OpenAI, OpenRouter), and build the mod queue, all runnable from the CLI.

**Architecture:** A deterministic data plane (SQLite + plain Python) with LLM calls isolated behind one provider-neutral `LLMClient` that validates structured output, retries, logs cost, and enforces a daily budget. The triage agent fans batches out to concurrent subagent calls; all DB writes other than run logging happen on the main thread after the calls finish.

**Tech Stack:** Python 3.12, sqlite3 (stdlib), tomllib (stdlib), `anthropic`, `openai`, `jsonschema`, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-discord-pulse-design.md`

**Plan series:** Plan 1 (this) = core pipeline. Plan 2 = theme, digest, investigate agents + stats. Plan 3 = FastAPI dashboard with message cards, jump links, all views, `seed-demo`. Plan 4 = eval harness, bot adapter, `docs/bot-pitch.md`, launchd jobs.

## Global Constraints

- Python `>=3.12`; create the venv with `uv venv --python 3.12 .venv` and install with `uv pip install -e '.[dev]'`; run tests with `.venv/bin/pytest`.
- Package `pulse/` lives at the repo root; tests live in `tests/`. Tests never touch the network.
- All timestamps are stored as UTC strings in the fixed-width format `%Y-%m-%dT%H:%M:%S.%fZ`, produced only by `pulse.models.to_iso`. Never store `datetime.isoformat()` output (variable width breaks lexical ordering).
- Secrets only from environment variables: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `DISCORD_BOT_TOKEN`. Never in `pulse.toml`.
- Model references are `"provider:model"` with provider in `anthropic | openai | openrouter`, split on the first colon.
- Agent modules never import a provider SDK; they call `LLMClient.complete`.
- The tool never reads Discord with a user account token.
- Every commit message ends with the line `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Spec clarifications made by this plan (treat as spec): optional `[paths]` config section (`db`, `imports`, relative to the config file); `messages.is_bot` column; `mod_queue.queue_key` and `mod_queue.closed_by` columns; mod queue only considers messages from the last 7 days (`QUEUE_LOOKBACK_DAYS = 7`); Anthropic cache-write tokens are billed at the input rate.

## Review Focus

1. First import of months of history must not flood the mod queue with stale items: only messages from the last 7 days are queued (Task 9 test `test_messages_older_than_lookback_are_ignored`).
2. Exports with mixed UTC offsets and 7-digit fractional seconds (DiscordChatExporter's .NET format) must normalize to fixed-width UTC so ordering and reply windows are correct (Task 2 `test_parse_timestamp_normalizes_offsets_and_long_fractions`, Task 4 `test_reads_dce_channel_export`).
3. Re-importing the same or an overlapping export must not duplicate messages; an edited message must be re-triaged (Task 3 `test_reimport_is_idempotent`, `test_edited_content_invalidates_triage`).
4. A model returning missing, extra, or duplicate message ids must never store a label on the wrong message: the batch is retried, then failed (Task 8 `test_batch_with_missing_ids_fails_without_storing`).
5. Hitting the budget cap mid-run must stop cleanly, record `skipped_budget`, and let the CLI exit normally with a message (Task 5 `test_budget_cap_blocks_call`, Task 8 `test_budget_cap_skips_all_batches`, Task 10 `test_format_report_mentions_budget`).

---

## File Structure

| File | Responsibility |
|---|---|
| `pyproject.toml`, `.gitignore`, `pulse.toml.example`, `README.md` | project setup, sample config, usage + ToS note |
| `pulse/config.py` | load and validate `pulse.toml` into a frozen `Config` |
| `pulse/models.py` | `Message`, `TriageResult`, `KINDS`, timestamp helpers |
| `pulse/links.py` | Discord jump-link construction |
| `pulse/db.py` | schema (all spec tables) and `connect()` |
| `pulse/store.py` | idempotent message upsert |
| `pulse/sources/base.py` | `Source` protocol |
| `pulse/sources/file_source.py` | DiscordChatExporter JSON + CSV import |
| `pulse/agents/base.py` | `Backend` protocol, `BackendResult`, error types |
| `pulse/pricing.py` | cost calculation |
| `pulse/agents/llm.py` | `LLMClient`: budget gate, retries, validation, run logging |
| `pulse/agents/providers/anthropic_backend.py` | Anthropic backend |
| `pulse/agents/providers/openai_backend.py` | OpenAI + OpenRouter backend |
| `pulse/agents/prompts/triage_v1.md` | triage system prompt |
| `pulse/agents/triage.py` | triage subagent fan-out and persistence |
| `pulse/modqueue.py` | deterministic mod queue rules |
| `pulse/pipeline.py` | wiring: backends, ingest, full pipeline, report formatting |
| `pulse/run.py` | CLI entrypoint `python -m pulse.run` |
| `tests/fakes.py` | `FakeBackend`, `make_config`, `make_llm`, `msg`, `set_triage` |
| `tests/fixtures/*` | recorded export files |

---

### Task 1: Project scaffold and config loading

**Files:**
- Create: `pyproject.toml`, `.gitignore`, `pulse/__init__.py`, `pulse/config.py`, `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PROVIDERS: tuple[str, ...]`, `AGENTS: tuple[str, ...] = ("triage", "theme", "digest", "investigate")`, `KEY_ENV: dict[str, str]`
  - `class ConfigError(Exception)`
  - `@dataclass(frozen=True) ModelRef(provider: str, model: str)` with `ModelRef.parse(s: str) -> ModelRef` and `str(ref) == "provider:model"`
  - `@dataclass(frozen=True) Price(input: float, output: float, cache_read: float = 0.0)` (USD per million tokens)
  - `@dataclass(frozen=True) Launch(name: str, date: str, keywords: tuple[str, ...])`
  - `@dataclass(frozen=True) Config(guild_id: str, channel_ids: tuple[str, ...], team_member_ids: frozenset[str], reply_window_hours: float, frustration_threshold: int, models: dict[str, ModelRef], daily_usd_cap: float, pricing: dict[str, Price], launches: tuple[Launch, ...], db_path: Path, imports_dir: Path)`
  - `load_config(path: str | Path, env: Mapping[str, str] | None = None) -> Config`

- [ ] **Step 1: Create the project skeleton**

`pyproject.toml`:

```toml
[project]
name = "discord-pulse"
version = "0.1.0"
description = "DevRel Discord sentiment, pain-point and mod-queue dashboard driven by LLM subagents"
requires-python = ">=3.12"
dependencies = [
    "anthropic>=0.40",
    "openai>=1.50",
    "jsonschema>=4.21",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "httpx>=0.27"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
include = ["pulse*"]

[tool.setuptools.package-data]
"pulse.agents" = ["prompts/*.md"]

[tool.pytest.ini_options]
pythonpath = ["."]
testpaths = ["tests"]
```

`.gitignore`:

```
.venv/
__pycache__/
*.egg-info/
*.db
*.db-wal
*.db-shm
imports/
pulse.toml
.env
```

`pulse/__init__.py` and `tests/__init__.py`: empty files.

Run: `uv venv --python 3.12 .venv && uv pip install -e '.[dev]'`
Expected: installs without errors.

- [ ] **Step 2: Write the failing tests**

`tests/test_config.py`:

```python
import pytest

from pulse.config import ConfigError, ModelRef, load_config

BASE = '''
[server]
guild_id = "g1"
channel_ids = ["c1"]
team_member_ids = ["t1"]

[mod_queue]
reply_window_hours = 12
frustration_threshold = -2

[models]
triage = "anthropic:claude-haiku-4-5-20251001"
theme = "openai:gpt-x"
digest = "anthropic:claude-opus-5-5"
investigate = "openrouter:anthropic/claude-sonnet-5"

[budget]
daily_usd_cap = 5.0

[pricing."anthropic:claude-haiku-4-5-20251001"]
input = 1.0
output = 5.0
cache_read = 0.1

[pricing."anthropic:claude-opus-5-5"]
input = 5.0
output = 25.0

[pricing."openai:gpt-x"]
input = 1.25
output = 10.0

[[launches]]
name = "v2.0 SDK"
date = "2026-09-15"
keywords = ["v2", "migration"]
'''

ENV = {"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o", "OPENROUTER_API_KEY": "r"}


def write(tmp_path, text):
    path = tmp_path / "pulse.toml"
    path.write_text(text)
    return path


def test_loads_valid_config(tmp_path):
    cfg = load_config(write(tmp_path, BASE), env=ENV)
    assert cfg.guild_id == "g1"
    assert cfg.channel_ids == ("c1",)
    assert cfg.team_member_ids == frozenset({"t1"})
    assert cfg.reply_window_hours == 12.0
    assert cfg.frustration_threshold == -2
    assert cfg.models["investigate"] == ModelRef("openrouter", "anthropic/claude-sonnet-5")
    assert cfg.pricing["anthropic:claude-haiku-4-5-20251001"].cache_read == 0.1
    assert cfg.pricing["openai:gpt-x"].cache_read == 0.0
    assert cfg.daily_usd_cap == 5.0
    assert cfg.launches[0].keywords == ("v2", "migration")
    assert cfg.db_path == tmp_path / "pulse.db"
    assert cfg.imports_dir == tmp_path / "imports"


def test_model_ref_splits_on_first_colon():
    ref = ModelRef.parse("openrouter:openai/gpt-x:free")
    assert ref == ModelRef("openrouter", "openai/gpt-x:free")
    assert str(ref) == "openrouter:openai/gpt-x:free"


def test_missing_key_for_used_provider_names_env_var(tmp_path):
    env = dict(ENV)
    del env["OPENAI_API_KEY"]
    with pytest.raises(ConfigError, match="OPENAI_API_KEY"):
        load_config(write(tmp_path, BASE), env=env)


def test_empty_key_counts_as_missing(tmp_path):
    env = dict(ENV, OPENROUTER_API_KEY="")
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        load_config(write(tmp_path, BASE), env=env)


def test_unused_provider_key_not_required(tmp_path):
    text = BASE.replace('theme = "openai:gpt-x"', 'theme = "anthropic:claude-opus-5-5"')
    env = dict(ENV)
    del env["OPENAI_API_KEY"]
    load_config(write(tmp_path, text), env=env)


def test_missing_pricing_for_non_openrouter_model(tmp_path):
    text = BASE.replace('digest = "anthropic:claude-opus-5-5"', 'digest = "anthropic:claude-unpriced"')
    with pytest.raises(ConfigError, match="anthropic:claude-unpriced"):
        load_config(write(tmp_path, text), env=ENV)


def test_openrouter_model_needs_no_pricing(tmp_path):
    cfg = load_config(write(tmp_path, BASE), env=ENV)
    assert "openrouter:anthropic/claude-sonnet-5" not in cfg.pricing


def test_bad_provider_rejected(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, BASE.replace("openai:gpt-x", "gemini:x")), env=ENV)


def test_bad_launch_date_rejected(tmp_path):
    with pytest.raises(ConfigError, match="launch"):
        load_config(write(tmp_path, BASE.replace("2026-09-15", "Sept 15")), env=ENV)


def test_missing_guild_id(tmp_path):
    with pytest.raises(ConfigError, match="guild_id"):
        load_config(write(tmp_path, BASE.replace('guild_id = "g1"', "")), env=ENV)


def test_missing_agent_model(tmp_path):
    with pytest.raises(ConfigError, match="digest"):
        load_config(write(tmp_path, BASE.replace('digest = "anthropic:claude-opus-5-5"', "")), env=ENV)


def test_paths_section_overrides_defaults(tmp_path):
    cfg = load_config(write(tmp_path, BASE + '\n[paths]\ndb = "data/p.db"\nimports = "in"\n'), env=ENV)
    assert cfg.db_path == tmp_path / "data" / "p.db"
    assert cfg.imports_dir == tmp_path / "in"
```

- [ ] **Step 2b: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.config'`

- [ ] **Step 3: Implement `pulse/config.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore pulse/__init__.py pulse/config.py tests/__init__.py tests/test_config.py
git commit -m "feat: project scaffold and validated config loading" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Models, timestamps, jump links, and database schema

**Files:**
- Create: `pulse/models.py`, `pulse/links.py`, `pulse/db.py`
- Test: `tests/test_models.py`, `tests/test_db.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `KINDS = ("bug", "question", "feature_request", "docs", "praise", "other")`
  - `to_iso(dt: datetime) -> str` (raises `ValueError` on naive datetimes), `from_iso(s: str) -> datetime`, `parse_timestamp(s: str) -> datetime` (aware, naive treated as UTC)
  - `@dataclass(frozen=True) Message(id, guild_id, channel_id, author_id, author_name, content, created_at: datetime, channel_name="", thread_id=None, author_avatar_url=None, is_bot=False, edited_at=None, reply_to_id=None, source="file")`
  - `@dataclass(frozen=True) TriageResult(message_id: str, sentiment: int, confidence: float, kind: str, topics: tuple[str, ...], needs_reply: bool)`
  - `jump_link(guild_id: str, channel_id: str, message_id: str) -> str`
  - `connect(path: str | Path) -> sqlite3.Connection` (row_factory `sqlite3.Row`, foreign keys on, schema applied, `check_same_thread=False`)

- [ ] **Step 1: Write the failing tests**

`tests/test_models.py`:

```python
from datetime import datetime, timedelta, timezone

import pytest

from pulse.links import jump_link
from pulse.models import from_iso, parse_timestamp, to_iso


def test_to_iso_is_fixed_width_utc():
    a = to_iso(datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc))
    b = to_iso(datetime(2026, 9, 20, 10, 0, 0, 123456, tzinfo=timezone.utc))
    assert a == "2026-09-20T10:00:00.000000Z"
    assert len(a) == len(b)
    assert a < b


def test_to_iso_converts_offsets_to_utc():
    eastern = timezone(timedelta(hours=-4))
    assert to_iso(datetime(2026, 9, 20, 10, 0, tzinfo=eastern)) == "2026-09-20T14:00:00.000000Z"


def test_to_iso_rejects_naive():
    with pytest.raises(ValueError):
        to_iso(datetime(2026, 9, 20, 10, 0))


def test_from_iso_roundtrip():
    dt = datetime(2026, 9, 20, 10, 0, 0, 500, tzinfo=timezone.utc)
    assert from_iso(to_iso(dt)) == dt


def test_parse_timestamp_normalizes_offsets_and_long_fractions():
    dt = parse_timestamp("2026-09-20T10:00:00.1234567-04:00")
    assert to_iso(dt) == "2026-09-20T14:00:00.123456Z"
    assert to_iso(parse_timestamp("2026-09-21T09:00:00Z")) == "2026-09-21T09:00:00.000000Z"


def test_parse_timestamp_treats_naive_as_utc():
    assert to_iso(parse_timestamp("2026-09-21 09:00:00")) == "2026-09-21T09:00:00.000000Z"


def test_jump_link():
    assert jump_link("900", "300", "3001") == "https://discord.com/channels/900/300/3001"
```

`tests/test_db.py`:

```python
import sqlite3

import pytest

from pulse.db import connect

EXPECTED_TABLES = {
    "messages", "triage", "themes", "message_themes", "theme_events", "launches",
    "mod_queue", "digests", "investigations", "agent_runs",
}


def test_connect_creates_all_tables():
    conn = connect(":memory:")
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_connect_is_idempotent(tmp_path):
    path = tmp_path / "p.db"
    connect(path).close()
    conn = connect(path)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0


def test_foreign_keys_enforced():
    conn = connect(":memory:")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO triage (message_id, sentiment, confidence, kind, topics, needs_reply,"
            " prompt_version, created_at) VALUES ('nope', 0, 1.0, 'other', '[]', 0, 'v', 'x')"
        )


def test_sentiment_range_enforced():
    conn = connect(":memory:")
    conn.execute(
        "INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content,"
        " created_at, source) VALUES ('m', 'g', 'c', 'a', 'n', 'hi', 'x', 'file')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO triage (message_id, sentiment, confidence, kind, topics, needs_reply,"
            " prompt_version, created_at) VALUES ('m', 3, 1.0, 'other', '[]', 0, 'v', 'x')"
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_models.py tests/test_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.links'`

- [ ] **Step 3: Implement**

`pulse/models.py`:

```python
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
    edited_at: datetime | None = None
    reply_to_id: str | None = None
    source: str = "file"


@dataclass(frozen=True)
class TriageResult:
    message_id: str
    sentiment: int
    confidence: float
    kind: str
    topics: tuple[str, ...]
    needs_reply: bool
```

`pulse/links.py`:

```python
"""Discord jump links. For a message in a thread, channel_id is the thread id."""


def jump_link(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
```

`pulse/db.py`:

```python
"""SQLite schema and connection."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    guild_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    channel_name TEXT NOT NULL DEFAULT '',
    thread_id TEXT,
    author_id TEXT NOT NULL,
    author_name TEXT NOT NULL,
    author_avatar_url TEXT,
    is_team INTEGER NOT NULL DEFAULT 0,
    is_bot INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    edited_at TEXT,
    reply_to_id TEXT,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_messages_reply ON messages(reply_to_id);
CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(author_id);

CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    status TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'skipped_budget')),
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_started ON agent_runs(started_at);

CREATE TABLE IF NOT EXISTS triage (
    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    sentiment INTEGER NOT NULL CHECK (sentiment BETWEEN -2 AND 2),
    confidence REAL NOT NULL,
    kind TEXT NOT NULL,
    topics TEXT NOT NULL,
    needs_reply INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'merged')),
    merged_into INTEGER REFERENCES themes(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_themes (
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    theme_id INTEGER NOT NULL REFERENCES themes(id),
    PRIMARY KEY (message_id, theme_id)
);

CREATE TABLE IF NOT EXISTS theme_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('create', 'rename', 'merge')),
    payload TEXT NOT NULL,
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS launches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    date TEXT NOT NULL,
    keywords TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mod_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_key TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id),
    thread_id TEXT,
    reason TEXT NOT NULL CHECK (reason IN ('unanswered', 'frustrated')),
    status TEXT NOT NULL CHECK (status IN ('open', 'handled', 'dismissed')),
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    closed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_mod_queue_key ON mod_queue(queue_key, status);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('weekly', 'launch')),
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    launch_id INTEGER REFERENCES launches(id),
    markdown TEXT NOT NULL,
    cited_message_ids TEXT NOT NULL,
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS investigations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    context TEXT NOT NULL,
    markdown TEXT,
    cited_message_ids TEXT NOT NULL DEFAULT '[]',
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False: LLMClient logs agent_runs from worker threads,
    # serialized by its own lock. All other writes happen on the main thread.
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_models.py tests/test_db.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add pulse/models.py pulse/links.py pulse/db.py tests/test_models.py tests/test_db.py
git commit -m "feat: data models, UTC timestamp helpers, jump links, SQLite schema" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Idempotent message upsert and shared test fakes

**Files:**
- Create: `pulse/store.py`, `tests/fakes.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: `Message`, `to_iso` (Task 2); `connect` (Task 2); `Config`, `ModelRef`, `Price`, `AGENTS` (Task 1).
- Produces:
  - `@dataclass UpsertStats(inserted: int = 0, updated: int = 0, unchanged: int = 0)`
  - `upsert_messages(conn, messages: Iterable[Message], team_ids: frozenset[str]) -> UpsertStats`: inserts new rows; for existing ids refreshes all columns (including `is_team`); when content changed, deletes the message's `triage` row and counts it as `updated`.
  - `tests/fakes.py`: `T0` (2026-09-28 12:00 UTC), `msg(...)`, `make_config(**overrides)`, `set_triage(conn, message_id, *, sentiment=0, needs_reply=False, kind="question")`. (`FakeBackend` and `make_llm` are added in Task 5.)

- [ ] **Step 1: Write `tests/fakes.py`**

```python
"""Shared test helpers. No network, no real providers."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pulse.config import AGENTS, Config, ModelRef, Price
from pulse.models import Message, to_iso

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


def msg(
    id: str,
    content: str = "hello",
    *,
    minutes: float = 0,
    channel_id: str = "100",
    thread_id: str | None = None,
    author_id: str = "u1",
    author_name: str = "alice",
    reply_to_id: str | None = None,
    is_bot: bool = False,
) -> Message:
    return Message(
        id=id,
        guild_id="900",
        channel_id=channel_id,
        channel_name="help",
        author_id=author_id,
        author_name=author_name,
        content=content,
        created_at=T0 + timedelta(minutes=minutes),
        thread_id=thread_id,
        reply_to_id=reply_to_id,
        is_bot=is_bot,
    )


def make_config(**overrides) -> Config:
    base = dict(
        guild_id="900",
        channel_ids=(),
        team_member_ids=frozenset({"t1"}),
        reply_window_hours=12.0,
        frustration_threshold=-2,
        models={a: ModelRef("anthropic", f"m-{a}") for a in AGENTS},
        daily_usd_cap=5.0,
        pricing={f"anthropic:m-{a}": Price(1.0, 5.0, 0.1) for a in AGENTS},
        launches=(),
        db_path=Path(":memory:"),
        imports_dir=Path("imports"),
    )
    base.update(overrides)
    return Config(**base)


def set_triage(conn, message_id: str, *, sentiment: int = 0, needs_reply: bool = False, kind: str = "question") -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO triage (message_id, sentiment, confidence, kind, topics,"
            " needs_reply, prompt_version, created_at) VALUES (?, ?, 0.9, ?, ?, ?, 'test', ?)",
            (message_id, sentiment, kind, json.dumps([]), int(needs_reply), to_iso(T0)),
        )
```

- [ ] **Step 2: Write the failing tests**

`tests/test_store.py`:

```python
from dataclasses import replace

from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import msg, set_triage

TEAM = frozenset({"t1"})


def test_inserts_new_messages_and_marks_team():
    conn = connect(":memory:")
    stats = upsert_messages(conn, [msg("m1"), msg("m2", author_id="t1")], TEAM)
    assert (stats.inserted, stats.updated, stats.unchanged) == (2, 0, 0)
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM messages")}
    assert rows["m1"]["is_team"] == 0
    assert rows["m2"]["is_team"] == 1
    assert rows["m1"]["created_at"] == "2026-09-28T12:00:00.000000Z"


def test_reimport_is_idempotent():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1"), msg("m2")], TEAM)
    stats = upsert_messages(conn, [msg("m1"), msg("m2")], TEAM)
    assert (stats.inserted, stats.updated, stats.unchanged) == (0, 0, 2)
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2


def test_edited_content_invalidates_triage():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", "broken")], TEAM)
    set_triage(conn, "m1", sentiment=-1)
    stats = upsert_messages(conn, [msg("m1", "fixed now, thanks")], TEAM)
    assert stats.updated == 1
    assert conn.execute("SELECT content FROM messages WHERE id='m1'").fetchone()[0] == "fixed now, thanks"
    assert conn.execute("SELECT count(*) FROM triage").fetchone()[0] == 0


def test_unchanged_content_keeps_triage_but_refreshes_team_flag():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1", author_id="u9")], TEAM)
    set_triage(conn, "m1")
    upsert_messages(conn, [msg("m1", author_id="u9")], frozenset({"u9"}))
    assert conn.execute("SELECT is_team FROM messages WHERE id='m1'").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM triage").fetchone()[0] == 1


def test_stores_optional_fields():
    conn = connect(":memory:")
    m = replace(msg("m1", thread_id="300", reply_to_id="m0", is_bot=True), author_avatar_url="https://x/a.png")
    upsert_messages(conn, [m], TEAM)
    row = conn.execute("SELECT * FROM messages").fetchone()
    assert (row["thread_id"], row["reply_to_id"], row["is_bot"], row["author_avatar_url"]) == (
        "300", "m0", 1, "https://x/a.png"
    )
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.store'`

- [ ] **Step 4: Implement `pulse/store.py`**

```python
"""Idempotent persistence of ingested messages."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable

from pulse.models import Message, to_iso

_COLUMNS = (
    "guild_id", "channel_id", "channel_name", "thread_id", "author_id", "author_name",
    "author_avatar_url", "is_team", "is_bot", "content", "created_at", "edited_at",
    "reply_to_id", "source",
)
_INSERT = (
    f"INSERT INTO messages (id, {', '.join(_COLUMNS)}) "
    f"VALUES (?, {', '.join('?' for _ in _COLUMNS)})"
)
_UPDATE = f"UPDATE messages SET {', '.join(f'{c} = ?' for c in _COLUMNS)} WHERE id = ?"


@dataclass
class UpsertStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def _values(m: Message, team_ids: frozenset[str]) -> tuple:
    return (
        m.guild_id, m.channel_id, m.channel_name, m.thread_id, m.author_id, m.author_name,
        m.author_avatar_url, int(m.author_id in team_ids), int(m.is_bot), m.content,
        to_iso(m.created_at), to_iso(m.edited_at) if m.edited_at else None,
        m.reply_to_id, m.source,
    )


def upsert_messages(
    conn: sqlite3.Connection, messages: Iterable[Message], team_ids: frozenset[str]
) -> UpsertStats:
    stats = UpsertStats()
    with conn:
        for m in messages:
            existing = conn.execute("SELECT content FROM messages WHERE id = ?", (m.id,)).fetchone()
            values = _values(m, team_ids)
            if existing is None:
                conn.execute(_INSERT, (m.id, *values))
                stats.inserted += 1
                continue
            conn.execute(_UPDATE, (*values, m.id))
            if existing["content"] != m.content:
                # Edited message: its old labels no longer apply.
                conn.execute("DELETE FROM triage WHERE message_id = ?", (m.id,))
                stats.updated += 1
            else:
                stats.unchanged += 1
    return stats
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_store.py -v`
Expected: 5 passed

- [ ] **Step 6: Commit**

```bash
git add pulse/store.py tests/fakes.py tests/test_store.py
git commit -m "feat: idempotent message upsert with triage invalidation on edit" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: File import source (DiscordChatExporter JSON + CSV)

**Files:**
- Create: `pulse/sources/__init__.py` (empty), `pulse/sources/base.py`, `pulse/sources/file_source.py`
- Create fixtures: `tests/fixtures/dce_channel.json`, `tests/fixtures/dce_thread.json`, `tests/fixtures/messages.csv`
- Test: `tests/test_file_source.py`

**Interfaces:**
- Consumes: `Message`, `parse_timestamp` (Task 2).
- Produces:
  - `class Source(Protocol)`: attribute `errors: list[str]`; method `fetch(self, since: datetime | None = None) -> Iterator[Message]`
  - `class FileSource(imports_dir: Path)`: `fetch(since=None)` yields messages from every `*.json` and `*.csv` in `imports_dir` (sorted by filename). `errors` is reset at the start of each `fetch` and holds one human-readable line per skipped file or row, naming the file (and CSV line number).
  - `CSV_REQUIRED = ("guild_id", "channel_id", "message_id", "author_id", "author_name", "content", "created_at")`
  - `DCE_MESSAGE_TYPES = ("Default", "Reply")`

- [ ] **Step 1: Create the fixtures**

`tests/fixtures/dce_channel.json`:

```json
{
  "guild": {"id": "900", "name": "Acme Dev", "iconUrl": ""},
  "channel": {"id": "100", "type": "GuildTextChat", "categoryId": "50", "category": "Community", "name": "help", "topic": null},
  "dateRange": {"after": null, "before": null},
  "messages": [
    {
      "id": "1001", "type": "Default",
      "timestamp": "2026-09-20T10:00:00.1234567-04:00", "timestampEdited": null, "isPinned": false,
      "content": "Install fails on M1 with the new SDK",
      "author": {"id": "u1", "name": "alice", "discriminator": "0000", "nickname": "Alice", "isBot": false,
                 "avatarUrl": "https://cdn.discordapp.com/avatars/u1/a.png"},
      "attachments": [], "embeds": [], "reactions": [], "mentions": []
    },
    {
      "id": "1002", "type": "Reply",
      "timestamp": "2026-09-20T14:30:00+00:00", "timestampEdited": "2026-09-20T14:35:00+00:00", "isPinned": false,
      "content": "Which Python version?",
      "author": {"id": "t1", "name": "staffer", "nickname": "Staffer", "isBot": false, "avatarUrl": "avatars/t1.png"},
      "reference": {"messageId": "1001", "channelId": "100", "guildId": "900"}
    },
    {
      "id": "1003", "type": "ChannelPinnedMessage",
      "timestamp": "2026-09-20T15:00:00+00:00", "content": "Pinned a message.",
      "author": {"id": "t1", "name": "staffer", "nickname": "Staffer", "isBot": false}
    },
    {
      "id": "1004", "type": "Default",
      "timestamp": "2026-09-20T15:05:00+00:00", "content": "Build passed",
      "author": {"id": "b1", "name": "ci-bot", "nickname": "ci-bot", "isBot": true}
    }
  ]
}
```

`tests/fixtures/dce_thread.json`:

```json
{
  "guild": {"id": "900", "name": "Acme Dev"},
  "channel": {"id": "300", "type": "GuildPublicThread", "categoryId": "100", "category": "help", "name": "M1 install thread"},
  "messages": [
    {
      "id": "3001", "type": "Default",
      "timestamp": "2026-09-20T16:00:00+00:00",
      "content": "Still broken after reinstall, any ideas?",
      "author": {"id": "u1", "name": "alice", "nickname": null, "isBot": false}
    }
  ]
}
```

`tests/fixtures/messages.csv`:

```
guild_id,channel_id,message_id,author_id,author_name,content,created_at,reply_to_id
900,100,2001,u2,bob,"Docs for auth are confusing, love the CLI though",2026-09-21T09:00:00Z,
900,100,2002,u3,carol,no timestamp,,
900,100,2003,u2,bob,thanks!,2026-09-21T09:05:00Z,2001
```

- [ ] **Step 2: Write the failing tests**

`tests/test_file_source.py`:

```python
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pulse.models import to_iso
from pulse.sources.file_source import FileSource

FIXTURES = Path(__file__).parent / "fixtures"


def imports_with(tmp_path, *names):
    for name in names:
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path


def by_id(messages):
    return {m.id: m for m in messages}


def test_reads_dce_channel_export(tmp_path):
    src = FileSource(imports_with(tmp_path, "dce_channel.json"))
    msgs = by_id(src.fetch())
    assert set(msgs) == {"1001", "1002", "1004"}  # pinned-message system event skipped
    alice = msgs["1001"]
    assert (alice.guild_id, alice.channel_id, alice.channel_name) == ("900", "100", "help")
    assert alice.author_name == "Alice"  # nickname preferred
    assert alice.author_avatar_url == "https://cdn.discordapp.com/avatars/u1/a.png"
    assert to_iso(alice.created_at) == "2026-09-20T14:00:00.123456Z"
    assert alice.thread_id is None
    staff = msgs["1002"]
    assert staff.reply_to_id == "1001"
    assert staff.author_avatar_url is None  # local media path dropped
    assert to_iso(staff.edited_at) == "2026-09-20T14:35:00.000000Z"
    assert msgs["1004"].is_bot is True
    assert src.errors == []


def test_thread_export_sets_thread_id(tmp_path):
    msgs = by_id(FileSource(imports_with(tmp_path, "dce_thread.json")).fetch())
    m = msgs["3001"]
    assert (m.channel_id, m.thread_id) == ("300", "300")
    assert m.author_name == "alice"  # null nickname falls back to name


def test_reads_csv_and_reports_bad_row_with_line(tmp_path):
    src = FileSource(imports_with(tmp_path, "messages.csv"))
    msgs = by_id(src.fetch())
    assert set(msgs) == {"2001", "2003"}
    assert msgs["2003"].reply_to_id == "2001"
    assert msgs["2001"].reply_to_id is None
    assert msgs["2001"].content == "Docs for auth are confusing, love the CLI though"
    assert len(src.errors) == 1
    assert "messages.csv:3" in src.errors[0]
    assert "created_at" in src.errors[0]


def test_bad_json_file_is_skipped_and_others_proceed(tmp_path):
    imports_with(tmp_path, "dce_thread.json")
    (tmp_path / "bad.json").write_text("{not json")
    src = FileSource(tmp_path)
    msgs = by_id(src.fetch())
    assert set(msgs) == {"3001"}
    assert len(src.errors) == 1 and "bad.json" in src.errors[0]


def test_json_that_is_not_a_dce_export(tmp_path):
    (tmp_path / "other.json").write_text('{"hello": "world"}')
    src = FileSource(tmp_path)
    assert list(src.fetch()) == []
    assert "other.json" in src.errors[0] and "DiscordChatExporter" in src.errors[0]


def test_csv_missing_required_columns(tmp_path):
    (tmp_path / "x.csv").write_text("a,b\n1,2\n")
    src = FileSource(tmp_path)
    assert list(src.fetch()) == []
    assert "x.csv" in src.errors[0] and "missing required columns" in src.errors[0]


def test_since_filters_and_other_files_ignored(tmp_path):
    imports_with(tmp_path, "dce_channel.json", "messages.csv")
    (tmp_path / "notes.txt").write_text("ignore me")
    since = datetime(2026, 9, 21, tzinfo=timezone.utc)
    assert set(by_id(FileSource(tmp_path).fetch(since))) == {"2001", "2003"}


def test_missing_imports_dir_yields_nothing(tmp_path):
    src = FileSource(tmp_path / "nope")
    assert list(src.fetch()) == []
    assert src.errors == []
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_file_source.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.sources'`

- [ ] **Step 4: Implement**

`pulse/sources/__init__.py`: empty.

`pulse/sources/base.py`:

```python
"""The one ingest interface. File import and the bot adapter both implement it."""
from __future__ import annotations

from datetime import datetime
from typing import Iterator, Protocol

from pulse.models import Message


class Source(Protocol):
    errors: list[str]

    def fetch(self, since: datetime | None = None) -> Iterator[Message]: ...
```

`pulse/sources/file_source.py`:

```python
"""Import Discord exports dropped into the imports folder.

Supports DiscordChatExporter JSON and a simple CSV. How the export files are
produced is outside this tool; it never reads Discord with a user token.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Iterator

from pulse.models import Message, parse_timestamp

CSV_REQUIRED = (
    "guild_id", "channel_id", "message_id", "author_id", "author_name", "content", "created_at",
)
DCE_MESSAGE_TYPES = ("Default", "Reply")


class FileSource:
    def __init__(self, imports_dir: Path):
        self.imports_dir = Path(imports_dir)
        self.errors: list[str] = []

    def fetch(self, since: datetime | None = None) -> Iterator[Message]:
        self.errors = []
        if not self.imports_dir.is_dir():
            return
        for path in sorted(self.imports_dir.iterdir()):
            suffix = path.suffix.lower()
            if suffix == ".json":
                messages = self._read_json(path)
            elif suffix == ".csv":
                messages = self._read_csv(path)
            else:
                continue
            for m in messages:
                if since is None or m.created_at >= since:
                    yield m

    def _read_json(self, path: Path) -> list[Message]:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            guild_id = str(data["guild"]["id"])
            channel = data["channel"]
            channel_id = str(channel["id"])
            raw_messages = data["messages"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            self.errors.append(f"{path.name}: cannot read JSON: {e}")
            return []
        except (KeyError, TypeError) as e:
            self.errors.append(f"{path.name}: not a DiscordChatExporter JSON export (missing {e})")
            return []

        is_thread = "Thread" in str(channel.get("type", ""))
        channel_name = str(channel.get("name") or "")
        out: list[Message] = []
        for i, raw in enumerate(raw_messages):
            try:
                if raw.get("type", "Default") not in DCE_MESSAGE_TYPES:
                    continue
                author = raw["author"]
                avatar = author.get("avatarUrl")
                reference = raw.get("reference") or {}
                edited = raw.get("timestampEdited")
                out.append(
                    Message(
                        id=str(raw["id"]),
                        guild_id=guild_id,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        thread_id=channel_id if is_thread else None,
                        author_id=str(author["id"]),
                        author_name=str(author.get("nickname") or author["name"]),
                        author_avatar_url=avatar if isinstance(avatar, str) and avatar.startswith("http") else None,
                        is_bot=bool(author.get("isBot", False)),
                        content=str(raw.get("content") or ""),
                        created_at=parse_timestamp(raw["timestamp"]),
                        edited_at=parse_timestamp(edited) if edited else None,
                        reply_to_id=str(reference["messageId"]) if reference.get("messageId") else None,
                        source="file",
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                self.errors.append(f"{path.name}: message #{i}: {e!r}")
        return out

    def _read_csv(self, path: Path) -> list[Message]:
        out: list[Message] = []
        try:
            with path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                missing = [c for c in CSV_REQUIRED if c not in (reader.fieldnames or [])]
                if missing:
                    self.errors.append(f"{path.name}: missing required columns {missing}")
                    return []
                for row in reader:
                    try:
                        out.append(_csv_row(row))
                    except ValueError as e:
                        self.errors.append(f"{path.name}:{reader.line_num}: {e}")
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            self.errors.append(f"{path.name}: {e}")
            return []
        return out


def _csv_row(row: dict[str, str | None]) -> Message:
    def val(key: str) -> str | None:
        return (row.get(key) or "").strip() or None

    for column in CSV_REQUIRED:
        if column != "content" and not val(column):
            raise ValueError(f"missing {column}")
    edited = val("edited_at")
    return Message(
        id=val("message_id"),
        guild_id=val("guild_id"),
        channel_id=val("channel_id"),
        channel_name=val("channel_name") or "",
        thread_id=val("thread_id"),
        author_id=val("author_id"),
        author_name=val("author_name"),
        author_avatar_url=val("author_avatar_url"),
        is_bot=(val("is_bot") or "").lower() in ("1", "true", "yes"),
        content=row.get("content") or "",
        created_at=parse_timestamp(val("created_at")),
        edited_at=parse_timestamp(edited) if edited else None,
        reply_to_id=val("reply_to_id"),
        source="file",
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_file_source.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add pulse/sources tests/fixtures tests/test_file_source.py
git commit -m "feat: file import source for DiscordChatExporter JSON and CSV" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Provider-neutral LLM client (budget, retries, validation, run logging)

**Files:**
- Create: `pulse/agents/__init__.py` (empty), `pulse/agents/base.py`, `pulse/pricing.py`, `pulse/agents/llm.py`
- Modify: `tests/fakes.py` (append `FakeBackend`, `make_llm`, `FIXED_NOW`)
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `Config`, `ModelRef`, `Price` (Task 1); `to_iso` (Task 2); `connect` (Task 2).
- Produces:
  - `pulse/agents/base.py`: `@dataclass(frozen=True) BackendResult(data: dict, input_tokens: int, output_tokens: int, cache_read_tokens: int = 0, reported_cost: float | None = None)`; `class Backend(Protocol): complete(self, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult`; exceptions `TransientError`, `ProviderError`, `OutputInvalid(message, input_tokens=0, output_tokens=0, cache_read_tokens=0, reported_cost=None)`, `BudgetExceeded`, `LLMError`. `input_tokens` means uncached input tokens; cached reads go in `cache_read_tokens`.
  - `pulse/pricing.py`: `cost_usd(price: Price | None, input_tokens: int, output_tokens: int, cache_read_tokens: int, reported: float | None) -> float`
  - `pulse/agents/llm.py`: `@dataclass(frozen=True) LLMResponse(data: dict, run_id: int)`; `class LLMClient(conn, config, backends: Mapping[str, Backend], *, now: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep)` with `spent_today() -> float` and `complete(agent: str, system: str, user: str, schema: dict, schema_name: str, validate: Callable[[dict], object] | None = None) -> LLMResponse` (raises `BudgetExceeded` or `LLMError`; every call writes exactly one `agent_runs` row).
  - `tests/fakes.py`: `FIXED_NOW`, `FakeBackend(responses=None, handler=None)` with `.calls: list[dict]`, `make_llm(conn, config, backend, *, sleeps: list | None = None) -> LLMClient`.

- [ ] **Step 1: Append to `tests/fakes.py`**

```python
from pulse.agents.base import BackendResult  # noqa: E402
from pulse.agents.llm import LLMClient  # noqa: E402

FIXED_NOW = datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)


class FakeBackend:
    """Replays queued responses, then falls back to handler(user) -> BackendResult.

    A queued item may be a BackendResult, an Exception (raised), or a callable
    taking the user prompt and returning either.
    """

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls: list[dict] = []

    def complete(self, model, system, user, schema, schema_name):
        self.calls.append({"model": model, "system": system, "user": user, "schema_name": schema_name})
        item = self.responses.pop(0) if self.responses else self.handler
        if item is None:
            raise AssertionError("FakeBackend has no response queued")
        if callable(item) and not isinstance(item, BackendResult):
            item = item(user)
        if isinstance(item, Exception):
            raise item
        return item


def make_llm(conn, config, backend, *, sleeps=None) -> LLMClient:
    sleeps = [] if sleeps is None else sleeps
    return LLMClient(conn, config, {"anthropic": backend}, now=lambda: FIXED_NOW, sleep=sleeps.append)
```

- [ ] **Step 2: Write the failing tests**

`tests/test_llm.py`:

```python
import pytest

from pulse.agents.base import (
    BackendResult, BudgetExceeded, LLMError, OutputInvalid, ProviderError, TransientError,
)
from pulse.db import connect
from pulse.models import to_iso
from tests.fakes import FIXED_NOW, FakeBackend, make_config, make_llm

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer"],
    "properties": {"answer": {"type": "string"}},
}


def ok(answer="yes", inp=1000, out=200, cache=0, cost=None):
    return BackendResult({"answer": answer}, inp, out, cache, cost)


def runs(conn):
    return conn.execute("SELECT * FROM agent_runs ORDER BY id").fetchall()


def call(llm, **kw):
    return llm.complete("triage", "sys", "user", SCHEMA, "answer_result", **kw)


def test_success_returns_data_and_logs_cost():
    conn = connect(":memory:")
    backend = FakeBackend([ok(inp=1000, out=200, cache=10_000)])
    resp = call(make_llm(conn, make_config(), backend))
    assert resp.data == {"answer": "yes"}
    [run] = runs(conn)
    assert resp.run_id == run["id"]
    assert (run["agent"], run["model"], run["status"]) == ("triage", "anthropic:m-triage", "ok")
    assert (run["input_tokens"], run["output_tokens"], run["cache_read_tokens"]) == (1000, 200, 10_000)
    # 1000*1.0 + 200*5.0 + 10000*0.1 per million
    assert run["cost_usd"] == pytest.approx(0.003)
    assert backend.calls[0]["model"] == "m-triage"


def test_reported_cost_overrides_pricing():
    conn = connect(":memory:")
    call(make_llm(conn, make_config(), FakeBackend([ok(cost=0.5)])))
    assert runs(conn)[0]["cost_usd"] == pytest.approx(0.5)


def test_schema_invalid_output_is_retried_once():
    conn = connect(":memory:")
    bad = BackendResult({"wrong": 1}, 100, 10)
    backend = FakeBackend([bad, ok()])
    assert call(make_llm(conn, make_config(), backend)).data == {"answer": "yes"}
    assert len(backend.calls) == 2
    [run] = runs(conn)
    assert run["input_tokens"] == 1100  # both attempts are billed


def test_invalid_twice_fails_and_records_error():
    conn = connect(":memory:")
    bad = BackendResult({"wrong": 1}, 100, 10)
    with pytest.raises(LLMError):
        call(make_llm(conn, make_config(), FakeBackend([bad, bad])))
    [run] = runs(conn)
    assert run["status"] == "failed"
    assert "invalid output" in run["error"]


def test_validate_callback_failure_triggers_retry():
    conn = connect(":memory:")
    backend = FakeBackend([ok("no"), ok("yes")])

    def must_be_yes(data):
        if data["answer"] != "yes":
            raise ValueError("answer must be yes")

    assert call(make_llm(conn, make_config(), backend), validate=must_be_yes).data == {"answer": "yes"}
    assert len(backend.calls) == 2


def test_output_invalid_tokens_are_billed():
    conn = connect(":memory:")
    backend = FakeBackend([OutputInvalid("no tool block", 500, 50), ok(inp=1000, out=200)])
    call(make_llm(conn, make_config(), backend))
    assert runs(conn)[0]["input_tokens"] == 1500


def test_transient_errors_back_off_then_succeed():
    conn = connect(":memory:")
    sleeps = []
    backend = FakeBackend([TransientError("429"), TransientError("503"), ok()])
    call(make_llm(conn, make_config(), backend, sleeps=sleeps))
    assert sleeps == [1, 2]
    assert runs(conn)[0]["status"] == "ok"


def test_transient_errors_exhausted_fail():
    conn = connect(":memory:")
    sleeps = []
    backend = FakeBackend([TransientError("429")] * 3)
    with pytest.raises(LLMError):
        call(make_llm(conn, make_config(), backend, sleeps=sleeps))
    assert len(backend.calls) == 3
    assert sleeps == [1, 2]
    assert runs(conn)[0]["status"] == "failed"


def test_provider_error_fails_without_retry():
    conn = connect(":memory:")
    backend = FakeBackend([ProviderError("401 bad key")])
    with pytest.raises(LLMError, match="bad key"):
        call(make_llm(conn, make_config(), backend))
    assert len(backend.calls) == 1


def insert_spend(conn, cost, when):
    with conn:
        conn.execute(
            "INSERT INTO agent_runs (agent, model, cost_usd, status, started_at) VALUES ('x', 'y', ?, 'ok', ?)",
            (cost, to_iso(when)),
        )


def test_budget_cap_blocks_call():
    conn = connect(":memory:")
    insert_spend(conn, 5.0, FIXED_NOW.replace(hour=1))
    backend = FakeBackend([ok()])
    with pytest.raises(BudgetExceeded):
        call(make_llm(conn, make_config(daily_usd_cap=5.0), backend))
    assert backend.calls == []
    assert runs(conn)[-1]["status"] == "skipped_budget"


def test_yesterdays_spend_does_not_count():
    conn = connect(":memory:")
    insert_spend(conn, 5.0, FIXED_NOW.replace(day=28))
    llm = make_llm(conn, make_config(daily_usd_cap=5.0), FakeBackend([ok()]))
    assert llm.spent_today() == 0.0
    call(llm)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_llm.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.agents'`

- [ ] **Step 4: Implement**

`pulse/agents/__init__.py`: empty.

`pulse/agents/base.py`:

```python
"""Provider-neutral backend contract and error types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class BackendResult:
    data: dict[str, Any]
    input_tokens: int  # uncached input tokens
    output_tokens: int
    cache_read_tokens: int = 0
    reported_cost: float | None = None  # provider-reported USD (OpenRouter)


class Backend(Protocol):
    def complete(
        self, model: str, system: str, user: str, schema: dict, schema_name: str
    ) -> BackendResult: ...


class TransientError(Exception):
    """Retryable provider failure: rate limit, 5xx, connection error."""


class ProviderError(Exception):
    """Non-retryable provider failure: bad request, auth, unknown model."""


class OutputInvalid(Exception):
    """The model answered, but the output could not be parsed. Carries usage so it is billed."""

    def __init__(
        self,
        message: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        reported_cost: float | None = None,
    ):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_tokens = cache_read_tokens
        self.reported_cost = reported_cost


class BudgetExceeded(Exception):
    """The daily USD cap is reached; no call was made."""


class LLMError(Exception):
    """The call failed after retries; recorded in agent_runs as failed."""
```

`pulse/pricing.py`:

```python
"""Cost of one LLM call in USD."""
from __future__ import annotations

from pulse.config import Price


def cost_usd(
    price: Price | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    reported: float | None,
) -> float:
    if reported is not None:
        return reported
    if price is None:
        return 0.0
    return (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read
    ) / 1_000_000
```

`pulse/agents/llm.py`:

```python
"""The single entry point agents use to call a model.

Resolves the agent's provider:model, enforces the daily budget, retries
transient errors with backoff, validates structured output (one retry),
and records exactly one agent_runs row per call.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

import jsonschema

from pulse.agents.base import (
    Backend, BackendResult, BudgetExceeded, LLMError, OutputInvalid, ProviderError, TransientError,
)
from pulse.config import Config, ModelRef, Price
from pulse.models import to_iso
from pulse.pricing import cost_usd

MAX_TRANSIENT_ATTEMPTS = 3
MAX_VALIDATION_ATTEMPTS = 2


@dataclass(frozen=True)
class LLMResponse:
    data: dict[str, Any]
    run_id: int


@dataclass
class _Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0

    def add(self, price: Price | None, inp: int, out: int, cache: int, reported: float | None) -> None:
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read_tokens += cache
        self.cost_usd += cost_usd(price, inp, out, cache, reported)


class LLMClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: Config,
        backends: Mapping[str, Backend],
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._conn = conn
        self._config = config
        self._backends = dict(backends)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        # Serializes this client's DB access across worker threads.
        self._lock = threading.Lock()

    def spent_today(self) -> float:
        midnight = self._now().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs WHERE started_at >= ?",
                (to_iso(midnight),),
            ).fetchone()
        return float(row[0])

    def complete(
        self,
        agent: str,
        system: str,
        user: str,
        schema: dict,
        schema_name: str,
        validate: Callable[[dict], object] | None = None,
    ) -> LLMResponse:
        ref = self._config.models[agent]
        price = self._config.pricing.get(str(ref))
        started = self._now()
        # Concurrent callers may each pass this check, so a run can overshoot
        # the cap by at most (concurrency - 1) calls.
        if self.spent_today() >= self._config.daily_usd_cap:
            self._record(agent, ref, started, "skipped_budget", "daily budget cap reached", _Usage())
            raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

        backend = self._backends[ref.provider]
        usage = _Usage()
        error = "no attempt made"
        for _ in range(MAX_VALIDATION_ATTEMPTS):
            try:
                result = self._call(backend, ref.model, system, user, schema, schema_name)
            except OutputInvalid as e:
                usage.add(price, e.input_tokens, e.output_tokens, e.cache_read_tokens, e.reported_cost)
                error = f"invalid output: {e}"
                continue
            except (TransientError, ProviderError) as e:
                error = f"provider error: {e}"
                break
            usage.add(price, result.input_tokens, result.output_tokens, result.cache_read_tokens, result.reported_cost)
            try:
                jsonschema.validate(result.data, schema)
                if validate is not None:
                    validate(result.data)
            except jsonschema.ValidationError as e:
                error = f"invalid output: {e.message}"
                continue
            except ValueError as e:
                error = f"invalid output: {e}"
                continue
            run_id = self._record(agent, ref, started, "ok", None, usage)
            return LLMResponse(result.data, run_id)

        self._record(agent, ref, started, "failed", error, usage)
        raise LLMError(f"{agent}: {error}")

    def _call(self, backend: Backend, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        for attempt in range(MAX_TRANSIENT_ATTEMPTS):
            try:
                return backend.complete(model, system, user, schema, schema_name)
            except TransientError:
                if attempt == MAX_TRANSIENT_ATTEMPTS - 1:
                    raise
                self._sleep(2**attempt)
        raise AssertionError("unreachable")

    def _record(self, agent: str, ref: ModelRef, started: datetime, status: str, error: str | None, usage: _Usage) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO agent_runs (agent, model, input_tokens, output_tokens, cache_read_tokens,"
                " cost_usd, status, error, started_at, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent, str(ref), usage.input_tokens, usage.output_tokens, usage.cache_read_tokens,
                    usage.cost_usd, status, error, to_iso(started), to_iso(self._now()),
                ),
            )
            return int(cur.lastrowid)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_llm.py -v`
Expected: 11 passed

- [ ] **Step 6: Commit**

```bash
git add pulse/agents/__init__.py pulse/agents/base.py pulse/pricing.py pulse/agents/llm.py tests/fakes.py tests/test_llm.py
git commit -m "feat: provider-neutral LLM client with budget gate, retries, validation, run logging" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Anthropic backend

**Files:**
- Create: `pulse/agents/providers/__init__.py` (empty), `pulse/agents/providers/anthropic_backend.py`
- Test: `tests/test_anthropic_backend.py`

**Interfaces:**
- Consumes: `BackendResult`, `OutputInvalid`, `ProviderError`, `TransientError` (Task 5).
- Produces: `class AnthropicBackend(client=None, *, max_tokens: int = 8192)` implementing `Backend`. Structured output via one forced tool named `schema_name` whose `input_schema` is the schema. System prompt sent with `cache_control: {"type": "ephemeral"}`. `input_tokens` = `usage.input_tokens + usage.cache_creation_input_tokens`; `cache_read_tokens` = `usage.cache_read_input_tokens`. HTTP 429/5xx/connection errors raise `TransientError`; other status errors raise `ProviderError`.

- [ ] **Step 1: Write the failing tests**

`tests/test_anthropic_backend.py`:

```python
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from pulse.agents.base import OutputInvalid, ProviderError, TransientError
from pulse.agents.providers.anthropic_backend import AnthropicBackend

REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"], "additionalProperties": False}


class FakeMessages:
    def __init__(self, outcome):
        self.outcome = outcome
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def backend_for(outcome):
    messages = FakeMessages(outcome)
    return AnthropicBackend(client=SimpleNamespace(messages=messages)), messages


def response(content, cache_read=900, cache_write=50):
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=20,
            cache_read_input_tokens=cache_read, cache_creation_input_tokens=cache_write,
        ),
    )


def test_returns_forced_tool_input_and_normalized_usage():
    resp = response([
        SimpleNamespace(type="text", text="ok"),
        SimpleNamespace(type="tool_use", name="answer_result", input={"answer": "x"}),
    ])
    backend, messages = backend_for(resp)
    result = backend.complete("claude-haiku-4-5-20251001", "sys", "hi", SCHEMA, "answer_result")
    assert result.data == {"answer": "x"}
    assert (result.input_tokens, result.output_tokens, result.cache_read_tokens) == (150, 20, 900)
    assert result.reported_cost is None
    kw = messages.kwargs
    assert kw["model"] == "claude-haiku-4-5-20251001"
    assert kw["tool_choice"] == {"type": "tool", "name": "answer_result"}
    assert kw["tools"][0]["input_schema"] == SCHEMA
    assert kw["system"][0] == {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    assert kw["messages"] == [{"role": "user", "content": "hi"}]


def test_missing_cache_fields_count_as_zero():
    resp = response([SimpleNamespace(type="tool_use", name="answer_result", input={"answer": "x"})], None, None)
    result = backend_for(resp)[0].complete("m", "sys", "hi", SCHEMA, "answer_result")
    assert (result.input_tokens, result.cache_read_tokens) == (100, 0)


def test_no_tool_block_is_output_invalid_with_usage():
    backend, _ = backend_for(response([SimpleNamespace(type="text", text="sorry")]))
    with pytest.raises(OutputInvalid) as exc:
        backend.complete("m", "sys", "hi", SCHEMA, "answer_result")
    assert exc.value.output_tokens == 20


def test_rate_limit_is_transient():
    err = anthropic.RateLimitError("slow down", response=httpx.Response(429, request=REQ), body=None)
    with pytest.raises(TransientError):
        backend_for(err)[0].complete("m", "sys", "hi", SCHEMA, "answer_result")


def test_server_error_is_transient():
    err = anthropic.InternalServerError("overloaded", response=httpx.Response(529, request=REQ), body=None)
    with pytest.raises(TransientError):
        backend_for(err)[0].complete("m", "sys", "hi", SCHEMA, "answer_result")


def test_connection_error_is_transient():
    with pytest.raises(TransientError):
        backend_for(anthropic.APIConnectionError(request=REQ))[0].complete("m", "sys", "hi", SCHEMA, "answer_result")


def test_auth_error_is_provider_error():
    err = anthropic.AuthenticationError("bad key", response=httpx.Response(401, request=REQ), body=None)
    with pytest.raises(ProviderError):
        backend_for(err)[0].complete("m", "sys", "hi", SCHEMA, "answer_result")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_anthropic_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.agents.providers'`

- [ ] **Step 3: Implement**

`pulse/agents/providers/__init__.py`: empty.

`pulse/agents/providers/anthropic_backend.py`:

```python
"""Anthropic backend: structured output via a single forced tool."""
from __future__ import annotations

import anthropic

from pulse.agents.base import BackendResult, OutputInvalid, ProviderError, TransientError


class AnthropicBackend:
    def __init__(self, client=None, *, max_tokens: int = 8192):
        self._client = client if client is not None else anthropic.Anthropic()
        self._max_tokens = max_tokens

    def complete(self, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                tools=[{"name": schema_name, "description": "Record the structured result.", "input_schema": schema}],
                tool_choice={"type": "tool", "name": schema_name},
            )
        except anthropic.APIConnectionError as e:
            raise TransientError(str(e)) from e
        except anthropic.APIStatusError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise ProviderError(str(e)) from e

        usage = resp.usage
        # Cache writes are billed at the input rate (slight undercount of the write premium).
        input_tokens = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", None) or 0)
        cache_read = getattr(usage, "cache_read_input_tokens", None) or 0
        output_tokens = usage.output_tokens or 0

        block = next((b for b in resp.content if b.type == "tool_use" and b.name == schema_name), None)
        if block is None:
            raise OutputInvalid("no tool_use block in response", input_tokens, output_tokens, cache_read)
        return BackendResult(dict(block.input), input_tokens, output_tokens, cache_read)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_anthropic_backend.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add pulse/agents/providers tests/test_anthropic_backend.py
git commit -m "feat: Anthropic backend with forced-tool structured output and prompt caching" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: OpenAI and OpenRouter backend

**Files:**
- Create: `pulse/agents/providers/openai_backend.py`
- Test: `tests/test_openai_backend.py`

**Interfaces:**
- Consumes: `BackendResult`, `OutputInvalid`, `ProviderError`, `TransientError` (Task 5).
- Produces:
  - `OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"`
  - `strict_schema(schema: dict) -> dict`: deep copy with constraint keywords (`minimum`, `maximum`, `exclusiveMinimum`, `exclusiveMaximum`, `minItems`, `maxItems`, `minLength`, `maxLength`, `pattern`, `format`) removed, without touching property names. Local validation in `LLMClient` still enforces them.
  - `class OpenAIBackend(client=None, *, openrouter: bool = False)` implementing `Backend` via Chat Completions with `response_format` `json_schema` (strict). For OpenRouter: sends `extra_body={"usage": {"include": True}}`, reads `usage.cost` as `reported_cost`, and on a `ProviderError` (4xx) retries once in JSON mode with the schema appended to the system prompt. `input_tokens` = `prompt_tokens - cached_tokens`.

- [ ] **Step 1: Write the failing tests**

`tests/test_openai_backend.py`:

```python
import copy
from types import SimpleNamespace

import httpx
import openai
import pytest

from pulse.agents.base import OutputInvalid, ProviderError, TransientError
from pulse.agents.providers.openai_backend import OpenAIBackend, strict_schema

REQ = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["answer", "score", "tags", "pattern"],
    "properties": {
        "answer": {"type": "string"},
        "score": {"type": "integer", "minimum": -2, "maximum": 2},
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "pattern": {"type": "string"},
    },
}
DATA = '{"answer": "x", "score": 1, "tags": [], "pattern": "p"}'


class FakeCompletions:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def backend_for(*outcomes, openrouter=False):
    completions = FakeCompletions(outcomes)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return OpenAIBackend(client=client, openrouter=openrouter), completions


def response(content=DATA, refusal=None, cost=None, cached=800):
    usage = SimpleNamespace(
        prompt_tokens=1000, completion_tokens=50,
        prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
    )
    if cost is not None:
        usage.cost = cost
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, refusal=refusal))],
        usage=usage,
    )


def status_error(cls, code):
    return cls("err", response=httpx.Response(code, request=REQ), body=None)


def test_openai_structured_output_and_usage():
    backend, completions = backend_for(response())
    result = backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")
    assert result.data["answer"] == "x"
    assert (result.input_tokens, result.cache_read_tokens, result.output_tokens) == (200, 800, 50)
    assert result.reported_cost is None
    kw = completions.calls[0]
    assert kw["model"] == "gpt-x"
    assert kw["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    fmt = kw["response_format"]
    assert fmt["type"] == "json_schema"
    assert fmt["json_schema"]["name"] == "answer_result"
    assert fmt["json_schema"]["strict"] is True
    assert "extra_body" not in kw


def test_strict_schema_strips_constraints_but_keeps_property_names():
    original = copy.deepcopy(SCHEMA)
    stripped = strict_schema(SCHEMA)
    assert SCHEMA == original
    assert "minimum" not in stripped["properties"]["score"]
    assert "maxItems" not in stripped["properties"]["tags"]
    assert "pattern" in stripped["properties"]
    assert stripped["required"] == SCHEMA["required"]


def test_missing_usage_details_count_as_zero_cached():
    resp = response()
    resp.usage.prompt_tokens_details = None
    result = backend_for(resp)[0].complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")
    assert (result.input_tokens, result.cache_read_tokens) == (1000, 0)


def test_openrouter_reports_cost_and_requests_usage():
    backend, completions = backend_for(response(cost=0.0042), openrouter=True)
    result = backend.complete("anthropic/claude-sonnet-5", "sys", "hi", SCHEMA, "answer_result")
    assert result.reported_cost == pytest.approx(0.0042)
    assert completions.calls[0]["extra_body"] == {"usage": {"include": True}}


def test_openrouter_falls_back_to_json_mode_on_bad_request():
    backend, completions = backend_for(
        status_error(openai.BadRequestError, 400), response(), openrouter=True
    )
    result = backend.complete("some/model", "sys", "hi", SCHEMA, "answer_result")
    assert result.data["answer"] == "x"
    fallback = completions.calls[1]
    assert fallback["response_format"] == {"type": "json_object"}
    assert '"answer"' in fallback["messages"][0]["content"]


def test_openai_bad_request_is_provider_error():
    backend, _ = backend_for(status_error(openai.BadRequestError, 400))
    with pytest.raises(ProviderError):
        backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")


def test_rate_limit_and_connection_errors_are_transient():
    with pytest.raises(TransientError):
        backend_for(status_error(openai.RateLimitError, 429))[0].complete("gpt-x", "s", "u", SCHEMA, "n")
    with pytest.raises(TransientError):
        backend_for(openai.APIConnectionError(request=REQ))[0].complete("gpt-x", "s", "u", SCHEMA, "n")


def test_non_json_content_is_output_invalid_with_usage():
    backend, _ = backend_for(response(content="not json"))
    with pytest.raises(OutputInvalid) as exc:
        backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")
    assert exc.value.output_tokens == 50


def test_refusal_is_output_invalid():
    backend, _ = backend_for(response(content=None, refusal="I can't help"))
    with pytest.raises(OutputInvalid, match="refused"):
        backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")


def test_json_array_is_output_invalid():
    backend, _ = backend_for(response(content="[1, 2]"))
    with pytest.raises(OutputInvalid):
        backend.complete("gpt-x", "sys", "hi", SCHEMA, "answer_result")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_openai_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.agents.providers.openai_backend'`

- [ ] **Step 3: Implement `pulse/agents/providers/openai_backend.py`**

```python
"""OpenAI Chat Completions backend. Also serves OpenRouter via base_url."""
from __future__ import annotations

import json
import os
from typing import Any

import openai

from pulse.agents.base import BackendResult, OutputInvalid, ProviderError, TransientError

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

_CONSTRAINT_KEYWORDS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minItems", "maxItems", "minLength", "maxLength", "pattern", "format",
})


def strict_schema(schema: dict) -> dict:
    """Copy of schema without constraint keywords strict mode may reject.

    LLMClient validates the full schema locally, so nothing is lost.
    """

    def strip(node: Any, is_properties_map: bool = False) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if not is_properties_map and key in _CONSTRAINT_KEYWORDS:
                    continue
                out[key] = strip(value, is_properties_map=(key == "properties" and not is_properties_map))
            return out
        if isinstance(node, list):
            return [strip(v) for v in node]
        return node

    return strip(schema)


class OpenAIBackend:
    def __init__(self, client=None, *, openrouter: bool = False):
        self._openrouter = openrouter
        if client is None:
            client = (
                openai.OpenAI(base_url=OPENROUTER_BASE_URL, api_key=os.environ["OPENROUTER_API_KEY"])
                if openrouter
                else openai.OpenAI()
            )
        self._client = client

    def complete(self, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema_name, "schema": strict_schema(schema), "strict": True},
        }
        try:
            resp = self._create(model, system, user, response_format)
        except ProviderError:
            if not self._openrouter:
                raise
            # Some OpenRouter models reject json_schema; fall back to JSON mode.
            fallback_system = (
                f"{system}\n\nRespond with only a JSON object matching this JSON Schema:\n{json.dumps(schema)}"
            )
            resp = self._create(model, fallback_system, user, {"type": "json_object"})
        return self._parse(resp)

    def _create(self, model: str, system: str, user: str, response_format: dict):
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": response_format,
        }
        if self._openrouter:
            kwargs["extra_body"] = {"usage": {"include": True}}
        try:
            return self._client.chat.completions.create(**kwargs)
        except openai.APIConnectionError as e:
            raise TransientError(str(e)) from e
        except openai.APIStatusError as e:
            if e.status_code == 429 or e.status_code >= 500:
                raise TransientError(str(e)) from e
            raise ProviderError(str(e)) from e

    def _parse(self, resp) -> BackendResult:
        usage = resp.usage
        details = getattr(usage, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", None) or 0) if details is not None else 0
        input_tokens = (usage.prompt_tokens or 0) - cached
        output_tokens = usage.completion_tokens or 0
        cost = getattr(usage, "cost", None)
        reported = float(cost) if cost is not None else None

        def invalid(reason: str) -> OutputInvalid:
            return OutputInvalid(reason, input_tokens, output_tokens, cached, reported)

        message = resp.choices[0].message
        if getattr(message, "refusal", None):
            raise invalid(f"model refused: {message.refusal}")
        try:
            data = json.loads(message.content or "")
        except json.JSONDecodeError as e:
            raise invalid(f"not JSON: {e}") from e
        if not isinstance(data, dict):
            raise invalid("expected a JSON object")
        return BackendResult(data, input_tokens, output_tokens, cached, reported)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_openai_backend.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add pulse/agents/providers/openai_backend.py tests/test_openai_backend.py
git commit -m "feat: OpenAI and OpenRouter backend with strict JSON schema and reported cost" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Triage subagent

**Files:**
- Create: `pulse/agents/prompts/triage_v1.md`, `pulse/agents/triage.py`
- Test: `tests/test_triage.py`

**Interfaces:**
- Consumes: `LLMClient.complete`, `LLMResponse`, `BudgetExceeded`, `LLMError` (Task 5); `KINDS`, `TriageResult`, `to_iso` (Task 2); `upsert_messages` (Task 3); fakes `FakeBackend`, `make_llm`, `make_config`, `msg`, `T0` (Tasks 3, 5).
- Produces:
  - `PROMPT_VERSION = "triage-v1"`, `SCHEMA_NAME = "triage_result"`, `TRIAGE_SCHEMA: dict`, `BATCH_SIZE = 25`, `MAX_CONTENT_CHARS = 2000`, `CONTEXT_MESSAGES = 3`
  - `@dataclass TriageStats(triaged: int = 0, failed_batches: int = 0, skipped_budget_batches: int = 0)`
  - `select_untriaged(conn, since: datetime | None = None) -> list[sqlite3.Row]`
  - `build_batch_input(conn, rows) -> str` (JSON `{"messages": [{message_id, author, is_team, channel, content, reply_to, context}]}`)
  - `parse_results(data: dict, expected_ids: set[str]) -> list[TriageResult]` (raises `ValueError` on missing, extra, or duplicate ids)
  - `run_triage(conn, llm, *, batch_size=BATCH_SIZE, concurrency=4, since=None, force=False) -> TriageStats`

- [ ] **Step 1: Write the prompt `pulse/agents/prompts/triage_v1.md`**

```markdown
You are the triage analyst for a developer product's Discord community. You label messages so the community team can see how users feel, which pain points recur, and who needs a human reply.

You receive JSON: {"messages": [...]}. Each message has message_id, author, is_team (true = staff), channel, content, reply_to (the message it replies to, or null) and context (up to 3 earlier messages in the same channel, oldest first). Use reply_to and context only to understand the message; label only the message itself.

Return exactly one result per input message_id, no more and no fewer, with these fields:

- sentiment: integer from -2 to 2 describing the author's experience with the product.
  -2 angry or blocked ("third day I can't deploy, this is unusable")
  -1 frustrated, confused, or reporting something broken
   0 neutral: questions, information, chit-chat
  +1 positive
  +2 enthusiastic praise
- confidence: 0 to 1, how sure you are of the sentiment.
- kind: one of bug, question, feature_request, docs, praise, other.
  bug = something is broken or behaves wrongly. question = asking how to do something.
  feature_request = asking for something that does not exist. docs = docs missing, wrong, or confusing.
  praise = thanks or compliments. other = everything else.
- topics: 0 to 3 short lowercase noun phrases naming the product area ("m1 install", "auth docs", "rate limits"). Name the area, not the emotion. Use [] only for pure chit-chat.
- needs_reply: true only when a non-staff user asked a question or reported a problem and a staff reply would help. false for staff messages, thanks, chit-chat, and messages that are themselves answers.

Community tone:
- Developer slang is often positive: "this is sick", "insane speedup", "it just works lol" are +1 or +2.
- Sarcasm is usually negative: "love how the CLI eats my config every update" is -1, kind bug.
- "lol broke again" is -1, kind bug.
- An emoji alone ("🔥", "👀") is 0 unless the context makes it clearly positive.
- A polite question about something blocking the user is still -1.

Examples:
"anyone know why `acme login` hangs on WSL?" -> sentiment -1, kind question, topics ["wsl login"], needs_reply true
"the new dashboard is sick, way faster" -> sentiment 2, kind praise, topics ["dashboard performance"], needs_reply false
"would be great to have a python 3.13 wheel" -> sentiment 0, kind feature_request, topics ["python 3.13 support"], needs_reply false
"the auth quickstart skips the token step, took me an hour" -> sentiment -1, kind docs, topics ["auth quickstart"], needs_reply false
```

- [ ] **Step 2: Write the failing tests**

`tests/test_triage.py`:

```python
import json

from pulse.agents.base import BackendResult
from pulse.agents.triage import MAX_CONTENT_CHARS, PROMPT_VERSION, run_triage
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import T0, FakeBackend, make_config, make_llm, msg

TEAM = frozenset({"t1"})


def echo(user, drop=None, sentiment=-1):
    items = json.loads(user)["messages"]
    return BackendResult(
        {"results": [
            {"message_id": m["message_id"], "sentiment": sentiment, "confidence": 0.8, "kind": "bug",
             "topics": ["Install ", ""], "needs_reply": True}
            for m in items if m["message_id"] != drop
        ]},
        100, 20,
    )


def setup(messages, **config_overrides):
    conn = connect(":memory:")
    upsert_messages(conn, messages, TEAM)
    backend = FakeBackend(handler=echo)
    llm = make_llm(conn, make_config(**config_overrides), backend)
    return conn, backend, llm


def triage_rows(conn):
    return {r["message_id"]: r for r in conn.execute("SELECT * FROM triage")}


def payload_items(call):
    return {m["message_id"]: m for m in json.loads(call["user"])["messages"]}


def test_triages_eligible_messages_and_skips_bots_and_empty():
    conn, backend, llm = setup([msg("m1", "broken"), msg("m2", "   "), msg("m3", "beep", is_bot=True)])
    stats = run_triage(conn, llm)
    assert stats.triaged == 1
    row = triage_rows(conn)["m1"]
    assert (row["sentiment"], row["kind"], row["needs_reply"]) == (-1, "bug", 1)
    assert json.loads(row["topics"]) == ["install"]
    assert row["prompt_version"] == PROMPT_VERSION
    assert row["run_id"] is not None
    assert backend.calls[0]["schema_name"] == "triage_result"


def test_splits_into_batches():
    conn, backend, llm = setup([msg(f"m{i}", minutes=i) for i in range(5)])
    stats = run_triage(conn, llm, batch_size=2)
    assert stats.triaged == 5
    assert len(backend.calls) == 3


def test_payload_includes_reply_parent_and_preceding_context():
    conn, backend, llm = setup([
        msg("m1", "first", minutes=0),
        msg("m2", "second", minutes=1, author_id="t1", author_name="staffer"),
        msg("m3", "still broken?", minutes=2, reply_to_id="m1"),
        msg("x1", "other channel", minutes=1, channel_id="999"),
    ])
    run_triage(conn, llm)
    item = payload_items(backend.calls[0])["m3"]
    assert item["reply_to"] == {"author": "alice", "content": "first"}
    assert [c["content"] for c in item["context"]] == ["first", "second"]
    assert payload_items(backend.calls[0])["m2"]["is_team"] is True


def test_long_content_is_truncated_in_payload():
    conn, backend, llm = setup([msg("m1", "x" * 2500)])
    run_triage(conn, llm)
    assert len(payload_items(backend.calls[0])["m1"]["content"]) == MAX_CONTENT_CHARS + 1


def test_batch_with_missing_ids_fails_without_storing():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("m1"), msg("m2", minutes=1), msg("m3", minutes=2)], TEAM)
    backend = FakeBackend(handler=lambda user: echo(user, drop="m2"))
    stats = run_triage(conn, make_llm(conn, make_config(), backend), batch_size=1)
    assert stats.triaged == 2
    assert stats.failed_batches == 1
    assert set(triage_rows(conn)) == {"m1", "m3"}
    failed = conn.execute("SELECT error FROM agent_runs WHERE status = 'failed'").fetchone()
    assert "m2" in failed["error"]


def test_budget_cap_skips_all_batches():
    conn, backend, llm = setup([msg("m1"), msg("m2", minutes=1)], daily_usd_cap=0.0)
    stats = run_triage(conn, llm, batch_size=1)
    assert stats.skipped_budget_batches == 2
    assert stats.triaged == 0
    assert backend.calls == []


def test_second_run_is_a_no_op():
    conn, backend, llm = setup([msg("m1")])
    run_triage(conn, llm)
    stats = run_triage(conn, llm)
    assert stats.triaged == 0
    assert len(backend.calls) == 1


def test_force_since_retriages_only_the_range():
    conn, backend, llm = setup([msg("old", minutes=0), msg("new", minutes=120)])
    run_triage(conn, llm)
    stats = run_triage(conn, llm, since=T0.replace(hour=13), force=True)
    assert stats.triaged == 1
    assert list(payload_items(backend.calls[-1])) == ["new"]
    assert set(triage_rows(conn)) == {"old", "new"}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_triage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.agents.triage'`

- [ ] **Step 4: Implement `pulse/agents/triage.py`**

```python
"""Triage subagent: label every message with sentiment, kind, topics, needs_reply.

Batches fan out to concurrent LLM calls. Worker threads only call the model;
triage rows are written on the calling thread after all calls finish.
"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pulse.agents.base import BudgetExceeded, LLMError
from pulse.agents.llm import LLMClient, LLMResponse
from pulse.models import KINDS, TriageResult, to_iso

PROMPT_VERSION = "triage-v1"
SCHEMA_NAME = "triage_result"
BATCH_SIZE = 25
MAX_CONTENT_CHARS = 2000
CONTEXT_MESSAGES = 3

TRIAGE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["results"],
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["message_id", "sentiment", "confidence", "kind", "topics", "needs_reply"],
                "properties": {
                    "message_id": {"type": "string"},
                    "sentiment": {"type": "integer", "minimum": -2, "maximum": 2},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "topics": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
                    "needs_reply": {"type": "boolean"},
                },
            },
        }
    },
}


@dataclass
class TriageStats:
    triaged: int = 0
    failed_batches: int = 0
    skipped_budget_batches: int = 0


def load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "triage_v1.md").read_text(encoding="utf-8")


def select_untriaged(conn: sqlite3.Connection, since: datetime | None = None) -> list[sqlite3.Row]:
    sql = (
        "SELECT m.* FROM messages m LEFT JOIN triage t ON t.message_id = m.id"
        " WHERE t.message_id IS NULL AND m.is_bot = 0 AND trim(m.content) != ''"
    )
    params: list[str] = []
    if since is not None:
        sql += " AND m.created_at >= ?"
        params.append(to_iso(since))
    sql += " ORDER BY m.created_at, m.id"
    return conn.execute(sql, params).fetchall()


def _clip(text: str) -> str:
    return text if len(text) <= MAX_CONTENT_CHARS else text[:MAX_CONTENT_CHARS] + "…"


def build_batch_input(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> str:
    items = []
    for r in rows:
        reply_to = None
        if r["reply_to_id"]:
            parent = conn.execute(
                "SELECT author_name, content FROM messages WHERE id = ?", (r["reply_to_id"],)
            ).fetchone()
            if parent is not None:
                reply_to = {"author": parent["author_name"], "content": _clip(parent["content"])}
        preceding = conn.execute(
            "SELECT author_name, content FROM messages WHERE channel_id = ? AND created_at < ?"
            " ORDER BY created_at DESC LIMIT ?",
            (r["channel_id"], r["created_at"], CONTEXT_MESSAGES),
        ).fetchall()
        items.append({
            "message_id": r["id"],
            "author": r["author_name"],
            "is_team": bool(r["is_team"]),
            "channel": r["channel_name"],
            "content": _clip(r["content"]),
            "reply_to": reply_to,
            "context": [{"author": p["author_name"], "content": _clip(p["content"])} for p in reversed(preceding)],
        })
    return json.dumps({"messages": items}, ensure_ascii=False)


def parse_results(data: dict, expected_ids: set[str]) -> list[TriageResult]:
    results = data["results"]
    ids = [r["message_id"] for r in results]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate message_id in results")
    got = set(ids)
    if got != expected_ids:
        raise ValueError(
            f"message_id mismatch: missing {sorted(expected_ids - got)}, unexpected {sorted(got - expected_ids)}"
        )
    return [
        TriageResult(
            message_id=r["message_id"],
            sentiment=int(r["sentiment"]),
            confidence=float(r["confidence"]),
            kind=r["kind"],
            topics=tuple(t.strip().lower() for t in r["topics"] if t.strip()),
            needs_reply=bool(r["needs_reply"]),
        )
        for r in results
    ]


def run_triage(
    conn: sqlite3.Connection,
    llm: LLMClient,
    *,
    batch_size: int = BATCH_SIZE,
    concurrency: int = 4,
    since: datetime | None = None,
    force: bool = False,
) -> TriageStats:
    if force:
        with conn:
            if since is None:
                conn.execute("DELETE FROM triage")
            else:
                conn.execute(
                    "DELETE FROM triage WHERE message_id IN (SELECT id FROM messages WHERE created_at >= ?)",
                    (to_iso(since),),
                )

    rows = select_untriaged(conn, since)
    batches = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
    jobs = [({r["id"] for r in b}, build_batch_input(conn, b)) for b in batches]
    system = load_prompt()

    def call(job: tuple[set[str], str]) -> tuple[set[str], LLMResponse | None, str | None]:
        ids, user = job
        try:
            resp = llm.complete(
                "triage", system, user, TRIAGE_SCHEMA, SCHEMA_NAME,
                validate=lambda data: parse_results(data, ids),
            )
            return ids, resp, None
        except BudgetExceeded:
            return ids, None, "budget"
        except LLMError:
            return ids, None, "failed"

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        outcomes = list(pool.map(call, jobs))

    stats = TriageStats()
    now = to_iso(datetime.now(timezone.utc))
    for ids, resp, error in outcomes:
        if error == "budget":
            stats.skipped_budget_batches += 1
            continue
        if error == "failed":
            stats.failed_batches += 1
            continue
        results = parse_results(resp.data, ids)
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO triage (message_id, sentiment, confidence, kind, topics,"
                " needs_reply, prompt_version, run_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (r.message_id, r.sentiment, r.confidence, r.kind, json.dumps(list(r.topics)),
                     int(r.needs_reply), PROMPT_VERSION, resp.run_id, now)
                    for r in results
                ],
            )
        stats.triaged += len(results)
    return stats
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_triage.py -v`
Expected: 8 passed

- [ ] **Step 6: Commit**

```bash
git add pulse/agents/prompts/triage_v1.md pulse/agents/triage.py tests/test_triage.py
git commit -m "feat: triage subagent with batched concurrent calls and id-checked results" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Mod queue rules

**Files:**
- Create: `pulse/modqueue.py`
- Test: `tests/test_modqueue.py`

**Interfaces:**
- Consumes: `Config` (Task 1); `to_iso` (Task 2); fakes `msg`, `set_triage`, `make_config`, `T0` (Task 3); `upsert_messages` (Task 3).
- Produces:
  - `QUEUE_LOOKBACK_DAYS = 7`
  - `@dataclass ModQueueStats(opened: int = 0, updated: int = 0, auto_closed: int = 0)`
  - `refresh_mod_queue(conn, config: Config, now: datetime) -> ModQueueStats`

Rules (spec section 7 plus the lookback clarification):
- Only non-team, non-bot, triaged messages created within the last `QUEUE_LOOKBACK_DAYS` are candidates.
- **frustrated**: `sentiment <= config.frustration_threshold`, regardless of replies or age within the lookback.
- **unanswered**: `needs_reply = 1`, message older than `reply_window_hours`, and no team message after it that replies to it directly or sits in the same thread.
- `queue_key` = `thread_id` if present, else the message id. At most one open item per key; a later trigger in the same key moves the item to the later message, and `frustrated` wins over `unanswered`.
- A message already referenced by any queue item is never queued again; a key closed at time X is not reopened by messages created at or before X.
- Open `unanswered` items auto-close (`status='handled'`, `closed_by='auto'`) once a team reply exists.

- [ ] **Step 1: Write the failing tests**

`tests/test_modqueue.py`:

```python
from datetime import timedelta

from pulse.db import connect
from pulse.models import to_iso
from pulse.modqueue import refresh_mod_queue
from pulse.store import upsert_messages
from tests.fakes import T0, make_config, msg, set_triage

CONFIG = make_config()  # team = {"t1"}, window 12h, threshold -2
NOW = T0 + timedelta(hours=24)


def db(*messages):
    conn = connect(":memory:")
    upsert_messages(conn, messages, CONFIG.team_member_ids)
    return conn


def items(conn):
    return conn.execute("SELECT * FROM mod_queue ORDER BY id").fetchall()


def test_unanswered_question_past_window_is_queued():
    conn = db(msg("q1", "how do I auth?"))
    set_triage(conn, "q1", needs_reply=True)
    stats = refresh_mod_queue(conn, CONFIG, NOW)
    assert stats.opened == 1
    [item] = items(conn)
    assert (item["message_id"], item["reason"], item["status"], item["queue_key"]) == ("q1", "unanswered", "open", "q1")


def test_question_inside_window_is_not_queued_yet():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, T0 + timedelta(hours=2)).opened == 0


def test_direct_team_reply_prevents_queueing():
    conn = db(msg("q1"), msg("r1", minutes=30, author_id="t1", reply_to_id="q1"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 0


def test_team_message_in_same_thread_prevents_queueing():
    conn = db(msg("q1", thread_id="T"), msg("r1", minutes=30, author_id="t1", thread_id="T"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 0


def test_non_team_reply_does_not_count():
    conn = db(msg("q1"), msg("r1", minutes=30, author_id="u2", reply_to_id="q1"))
    set_triage(conn, "q1", needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 1


def test_frustrated_is_queued_even_if_young_and_answered():
    conn = db(msg("f1", "unusable"), msg("r1", minutes=5, author_id="t1", reply_to_id="f1"))
    set_triage(conn, "f1", sentiment=-2)
    refresh_mod_queue(conn, CONFIG, T0 + timedelta(minutes=10))
    [item] = items(conn)
    assert item["reason"] == "frustrated"


def test_team_messages_are_never_queued():
    conn = db(msg("t", "our fault, sorry", author_id="t1"))
    set_triage(conn, "t", sentiment=-2, needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, NOW).opened == 0


def test_one_open_item_per_thread_moves_to_later_trigger():
    conn = db(msg("a", thread_id="T"), msg("b", minutes=600, thread_id="T"))
    set_triage(conn, "a", needs_reply=True)
    set_triage(conn, "b", sentiment=-2)
    stats = refresh_mod_queue(conn, CONFIG, NOW)
    assert (stats.opened, stats.updated) == (1, 1)
    [item] = items(conn)
    assert (item["message_id"], item["reason"], item["queue_key"]) == ("b", "frustrated", "T")


def test_rerun_is_idempotent():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    refresh_mod_queue(conn, CONFIG, NOW)
    stats = refresh_mod_queue(conn, CONFIG, NOW + timedelta(minutes=30))
    assert (stats.opened, stats.updated) == (0, 0)
    assert len(items(conn)) == 1


def test_unanswered_auto_closes_when_team_replies_later():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    refresh_mod_queue(conn, CONFIG, NOW)
    upsert_messages(conn, [msg("r1", minutes=60 * 25, author_id="t1", reply_to_id="q1")], CONFIG.team_member_ids)
    stats = refresh_mod_queue(conn, CONFIG, NOW + timedelta(hours=2))
    assert stats.auto_closed == 1
    [item] = items(conn)
    assert (item["status"], item["closed_by"]) == ("handled", "auto")


def test_dismissed_item_is_not_reopened():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    refresh_mod_queue(conn, CONFIG, NOW)
    with conn:
        conn.execute(
            "UPDATE mod_queue SET status = 'dismissed', closed_at = ?, closed_by = 'user'", (to_iso(NOW),)
        )
    assert refresh_mod_queue(conn, CONFIG, NOW + timedelta(hours=1)).opened == 0


def test_messages_older_than_lookback_are_ignored():
    conn = db(msg("old", minutes=0))
    set_triage(conn, "old", sentiment=-2, needs_reply=True)
    assert refresh_mod_queue(conn, CONFIG, T0 + timedelta(days=8)).opened == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_modqueue.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.modqueue'`

- [ ] **Step 3: Implement `pulse/modqueue.py`**

```python
"""Deterministic mod queue: who needs a human reply."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from pulse.config import Config
from pulse.models import to_iso

# Keeps the first import of months of history from flooding the queue.
QUEUE_LOOKBACK_DAYS = 7


@dataclass
class ModQueueStats:
    opened: int = 0
    updated: int = 0
    auto_closed: int = 0


def _team_replied(conn: sqlite3.Connection, message_id: str, thread_id: str | None, created_at: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM messages r WHERE r.is_team = 1 AND r.created_at > ?"
        " AND (r.reply_to_id = ? OR (? IS NOT NULL AND r.thread_id = ?)) LIMIT 1",
        (created_at, message_id, thread_id, thread_id),
    ).fetchone()
    return row is not None


def refresh_mod_queue(conn: sqlite3.Connection, config: Config, now: datetime) -> ModQueueStats:
    stats = ModQueueStats()
    now_iso = to_iso(now)
    reply_cutoff = to_iso(now - timedelta(hours=config.reply_window_hours))
    lookback = to_iso(now - timedelta(days=QUEUE_LOOKBACK_DAYS))

    with conn:
        open_unanswered = conn.execute(
            "SELECT q.id, m.id AS message_id, m.thread_id, m.created_at FROM mod_queue q"
            " JOIN messages m ON m.id = q.message_id WHERE q.status = 'open' AND q.reason = 'unanswered'"
        ).fetchall()
        for item in open_unanswered:
            if _team_replied(conn, item["message_id"], item["thread_id"], item["created_at"]):
                conn.execute(
                    "UPDATE mod_queue SET status = 'handled', closed_at = ?, closed_by = 'auto' WHERE id = ?",
                    (now_iso, item["id"]),
                )
                stats.auto_closed += 1

        triggers = conn.execute(
            "SELECT m.id, m.thread_id, m.created_at, t.sentiment, t.needs_reply FROM messages m"
            " JOIN triage t ON t.message_id = m.id"
            " WHERE m.is_team = 0 AND m.is_bot = 0 AND m.created_at >= ?"
            " AND (t.sentiment <= ? OR (t.needs_reply = 1 AND m.created_at <= ?))"
            " ORDER BY m.created_at, m.id",
            (lookback, config.frustration_threshold, reply_cutoff),
        ).fetchall()

        for m in triggers:
            reason = "frustrated" if m["sentiment"] <= config.frustration_threshold else "unanswered"
            if reason == "unanswered" and _team_replied(conn, m["id"], m["thread_id"], m["created_at"]):
                continue
            if conn.execute("SELECT 1 FROM mod_queue WHERE message_id = ?", (m["id"],)).fetchone():
                continue
            key = m["thread_id"] or m["id"]
            open_item = conn.execute(
                "SELECT q.id, q.reason, m.created_at FROM mod_queue q JOIN messages m ON m.id = q.message_id"
                " WHERE q.queue_key = ? AND q.status = 'open'",
                (key,),
            ).fetchone()
            if open_item is not None:
                if m["created_at"] > open_item["created_at"]:
                    new_reason = "frustrated" if "frustrated" in (reason, open_item["reason"]) else "unanswered"
                    conn.execute(
                        "UPDATE mod_queue SET message_id = ?, reason = ? WHERE id = ?",
                        (m["id"], new_reason, open_item["id"]),
                    )
                    stats.updated += 1
                continue
            last_closed = conn.execute(
                "SELECT MAX(closed_at) FROM mod_queue WHERE queue_key = ? AND status != 'open'", (key,)
            ).fetchone()[0]
            if last_closed is not None and m["created_at"] <= last_closed:
                continue
            conn.execute(
                "INSERT INTO mod_queue (queue_key, message_id, thread_id, reason, status, opened_at)"
                " VALUES (?, ?, ?, ?, 'open', ?)",
                (key, m["id"], m["thread_id"], reason, now_iso),
            )
            stats.opened += 1
    return stats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_modqueue.py -v`
Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add pulse/modqueue.py tests/test_modqueue.py
git commit -m "feat: deterministic mod queue with thread dedupe, auto-close, and lookback" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 10: Pipeline wiring, CLI, sample config, README

**Files:**
- Create: `pulse/pipeline.py`, `pulse/run.py`, `pulse.toml.example`, `README.md`
- Test: `tests/test_pipeline.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: everything above: `load_config`, `ConfigError`, `Config` (1); `connect` (2); `upsert_messages`, `UpsertStats` (3); `FileSource`, `Source` (4); `LLMClient`, `Backend` (5); `AnthropicBackend` (6); `OpenAIBackend` (7); `run_triage`, `TriageStats` (8); `refresh_mod_queue`, `ModQueueStats` (9).
- Produces:
  - `build_backends(config: Config) -> dict[str, Backend]` (only providers in use)
  - `ingest(conn, config, source: Source) -> tuple[UpsertStats, list[str]]`
  - `@dataclass PipelineReport(ingest: UpsertStats, ingest_errors: list[str], triage: TriageStats, modqueue: ModQueueStats)`
  - `run_pipeline(conn, config, llm, *, source: Source | None = None, now: datetime | None = None) -> PipelineReport`
  - `format_report(report: PipelineReport) -> str`
  - `pulse.run.main(argv: list[str] | None = None) -> int` with subcommands `ingest`, `triage [--since YYYY-MM-DD] [--force]`, `modqueue`, `pipeline`; global `--config` (default `pulse.toml`). Exit code 2 on config error, 0 otherwise.

- [ ] **Step 1: Write the failing tests**

`tests/test_pipeline.py`:

```python
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pulse.agents.base import BackendResult
from pulse.agents.triage import TriageStats
from pulse.db import connect
from pulse.modqueue import ModQueueStats
from pulse.pipeline import PipelineReport, format_report, run_pipeline
from pulse.sources.file_source import FileSource
from pulse.store import UpsertStats
from tests.fakes import FakeBackend, make_config, make_llm

FIXTURES = Path(__file__).parent / "fixtures"


def label(user):
    results = []
    for m in json.loads(user)["messages"]:
        text = m["content"]
        sentiment = -2 if "fails" in text else -1 if "broken" in text else 0
        results.append({
            "message_id": m["message_id"], "sentiment": sentiment, "confidence": 0.9,
            "kind": "bug" if sentiment < 0 else "other", "topics": ["install"] if sentiment < 0 else [],
            "needs_reply": "?" in text,
        })
    return BackendResult({"results": results}, 100, 20)


def test_end_to_end_import_triage_and_queue(tmp_path):
    for name in ("dce_channel.json", "dce_thread.json", "messages.csv"):
        shutil.copy(FIXTURES / name, tmp_path / name)
    conn = connect(":memory:")
    config = make_config(imports_dir=tmp_path)
    llm = make_llm(conn, config, FakeBackend(handler=label))

    report = run_pipeline(
        conn, config, llm, source=FileSource(tmp_path),
        now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc),
    )

    assert report.ingest.inserted == 6
    assert len(report.ingest_errors) == 1  # messages.csv line 3
    assert report.triage.triaged == 5  # bot message 1004 skipped
    assert report.modqueue.opened == 2
    queue = {r["message_id"]: r["reason"] for r in conn.execute("SELECT * FROM mod_queue")}
    assert queue == {"1001": "frustrated", "3001": "unanswered"}


def test_format_report_mentions_budget():
    report = PipelineReport(
        ingest=UpsertStats(inserted=3), ingest_errors=["a.csv:2: missing created_at"],
        triage=TriageStats(triaged=1, failed_batches=1, skipped_budget_batches=2),
        modqueue=ModQueueStats(opened=1),
    )
    text = format_report(report)
    assert "inserted 3" in text
    assert "a.csv:2" in text
    assert "failed batches 1" in text
    assert "daily budget cap reached" in text
    assert "opened 1" in text
```

`tests/test_cli.py`:

```python
import shutil
from pathlib import Path

from pulse.db import connect
from pulse.run import main

FIXTURES = Path(__file__).parent / "fixtures"

CONFIG = '''
[server]
guild_id = "900"
team_member_ids = ["t1"]

[models]
triage = "anthropic:m"
theme = "anthropic:m"
digest = "anthropic:m"
investigate = "anthropic:m"

[pricing."anthropic:m"]
input = 1.0
output = 5.0
'''


def test_config_error_exits_2(tmp_path, capsys):
    (tmp_path / "pulse.toml").write_text("[server]\n")
    assert main(["--config", str(tmp_path / "pulse.toml"), "ingest"]) == 2
    assert "guild_id" in capsys.readouterr().err


def test_ingest_command_imports_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    (tmp_path / "imports").mkdir()
    shutil.copy(FIXTURES / "dce_channel.json", tmp_path / "imports" / "dce_channel.json")

    assert main(["--config", str(tmp_path / "pulse.toml"), "ingest"]) == 0

    out = capsys.readouterr().out
    assert "inserted 3" in out
    conn = connect(tmp_path / "pulse.db")
    assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
    assert conn.execute("SELECT is_team FROM messages WHERE id = '1002'").fetchone()[0] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_cli.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.pipeline'`

- [ ] **Step 3: Implement `pulse/pipeline.py`**

```python
"""Wires sources, agents, and rules into runnable stages. Plain code, no LLM decisions."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from pulse.agents.base import Backend
from pulse.agents.llm import LLMClient
from pulse.agents.triage import TriageStats, run_triage
from pulse.config import Config
from pulse.modqueue import ModQueueStats, refresh_mod_queue
from pulse.sources.base import Source
from pulse.sources.file_source import FileSource
from pulse.store import UpsertStats, upsert_messages


def build_backends(config: Config) -> dict[str, Backend]:
    # Imported here so a missing SDK only matters for providers actually in use.
    providers = {ref.provider for ref in config.models.values()}
    backends: dict[str, Backend] = {}
    if "anthropic" in providers:
        from pulse.agents.providers.anthropic_backend import AnthropicBackend

        backends["anthropic"] = AnthropicBackend()
    if "openai" in providers or "openrouter" in providers:
        from pulse.agents.providers.openai_backend import OpenAIBackend

        if "openai" in providers:
            backends["openai"] = OpenAIBackend()
        if "openrouter" in providers:
            backends["openrouter"] = OpenAIBackend(openrouter=True)
    return backends


def build_llm(conn: sqlite3.Connection, config: Config) -> LLMClient:
    return LLMClient(conn, config, build_backends(config))


def ingest(conn: sqlite3.Connection, config: Config, source: Source) -> tuple[UpsertStats, list[str]]:
    stats = upsert_messages(conn, source.fetch(None), config.team_member_ids)
    return stats, list(source.errors)


@dataclass
class PipelineReport:
    ingest: UpsertStats
    ingest_errors: list[str]
    triage: TriageStats
    modqueue: ModQueueStats


def run_pipeline(
    conn: sqlite3.Connection,
    config: Config,
    llm: LLMClient,
    *,
    source: Source | None = None,
    now: datetime | None = None,
) -> PipelineReport:
    source = source if source is not None else FileSource(config.imports_dir)
    now = now if now is not None else datetime.now(timezone.utc)
    ingest_stats, errors = ingest(conn, config, source)
    triage_stats = run_triage(conn, llm)
    queue_stats = refresh_mod_queue(conn, config, now)
    return PipelineReport(ingest_stats, errors, triage_stats, queue_stats)


def format_ingest(stats: UpsertStats, errors: list[str]) -> str:
    lines = [f"ingest: inserted {stats.inserted}, updated {stats.updated}, unchanged {stats.unchanged}"]
    lines += [f"  skipped: {e}" for e in errors]
    return "\n".join(lines)


def format_triage(stats: TriageStats) -> str:
    line = f"triage: triaged {stats.triaged}, failed batches {stats.failed_batches}"
    if stats.skipped_budget_batches:
        line += f"\n  daily budget cap reached: {stats.skipped_budget_batches} batches skipped"
    return line


def format_modqueue(stats: ModQueueStats) -> str:
    return f"mod queue: opened {stats.opened}, updated {stats.updated}, auto-closed {stats.auto_closed}"


def format_report(report: PipelineReport) -> str:
    return "\n".join([
        format_ingest(report.ingest, report.ingest_errors),
        format_triage(report.triage),
        format_modqueue(report.modqueue),
    ])
```

- [ ] **Step 4: Implement `pulse/run.py`**

```python
"""CLI: python -m pulse.run [--config pulse.toml] <command>."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from pulse.agents.triage import run_triage
from pulse.config import ConfigError, load_config
from pulse.db import connect
from pulse.modqueue import refresh_mod_queue
from pulse.pipeline import (
    build_llm, format_ingest, format_modqueue, format_report, format_triage, ingest, run_pipeline,
)
from pulse.sources.file_source import FileSource


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulse")
    parser.add_argument("--config", default="pulse.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="import export files from the imports folder")
    triage = sub.add_parser("triage", help="label untriaged messages")
    triage.add_argument("--since", type=date.fromisoformat, help="only messages on or after YYYY-MM-DD")
    triage.add_argument("--force", action="store_true", help="re-triage messages in range")
    sub.add_parser("modqueue", help="refresh the mod queue")
    sub.add_parser("pipeline", help="ingest, triage, then refresh the mod queue")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(config.db_path)
    now = datetime.now(timezone.utc)

    if args.command == "ingest":
        stats, errors = ingest(conn, config, FileSource(config.imports_dir))
        print(format_ingest(stats, errors))
    elif args.command == "triage":
        since = datetime.combine(args.since, datetime.min.time(), timezone.utc) if args.since else None
        print(format_triage(run_triage(conn, build_llm(conn, config), since=since, force=args.force)))
    elif args.command == "modqueue":
        print(format_modqueue(refresh_mod_queue(conn, config, now)))
    elif args.command == "pipeline":
        print(format_report(run_pipeline(conn, config, build_llm(conn, config), now=now)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pipeline.py tests/test_cli.py -v`
Expected: 4 passed

- [ ] **Step 6: Write `pulse.toml.example`**

```toml
# Copy to pulse.toml. Secrets go in environment variables, never here:
#   ANTHROPIC_API_KEY, OPENAI_API_KEY, OPENROUTER_API_KEY (only for providers you use)

[server]
guild_id = "123456789012345678"
channel_ids = []                     # empty = every channel in your exports
team_member_ids = []                 # staff user ids; their replies count as "answered"

[mod_queue]
reply_window_hours = 12
frustration_threshold = -2

[models]                             # "provider:model"; provider = anthropic | openai | openrouter
triage = "anthropic:claude-haiku-4-5-20251001"
theme = "anthropic:claude-sonnet-5"
digest = "anthropic:claude-opus-5-5"
investigate = "anthropic:claude-sonnet-5"

[budget]
daily_usd_cap = 5.00

# USD per million tokens. Required for every non-OpenRouter model above.
# These values are placeholders: fill in current prices.
[pricing."anthropic:claude-haiku-4-5-20251001"]
input = 1.00
output = 5.00
cache_read = 0.10

[pricing."anthropic:claude-sonnet-5"]
input = 3.00
output = 15.00
cache_read = 0.30

[pricing."anthropic:claude-opus-5-5"]
input = 5.00
output = 25.00
cache_read = 0.50

# [[launches]]
# name = "v2.0 SDK"
# date = "2026-09-15"
# keywords = ["v2", "migration"]

# [paths]
# db = "pulse.db"
# imports = "imports"
```

- [ ] **Step 7: Write `README.md`**

````markdown
# Discord Pulse

Internal DevRel tool: how a product's Discord community feels, which pain points recur, and who still needs a reply. Analysis is done by LLM subagents (Anthropic, OpenAI, or OpenRouter, chosen per agent).

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
cp pulse.toml.example pulse.toml   # then edit
export ANTHROPIC_API_KEY=...       # and/or OPENAI_API_KEY, OPENROUTER_API_KEY
```

## Getting messages in

Drop export files into `imports/`:

- DiscordChatExporter JSON (one file per channel or thread), or
- CSV with columns `guild_id,channel_id,message_id,author_id,author_name,content,created_at` and optional `reply_to_id,thread_id,channel_name,author_avatar_url,is_bot,edited_at`.

Re-importing is safe: messages are keyed on Discord's message id, and edited messages are re-triaged.

**Terms of Service warning:** this tool never reads Discord with a user account token. Exporters that run on your user token count as self-botting under Discord's Terms of Service and can get the account banned. The supported live route is a read-only bot added by a server admin (coming in a later release).

## Commands

```bash
.venv/bin/python -m pulse.run ingest              # import files from imports/
.venv/bin/python -m pulse.run triage              # label new messages
.venv/bin/python -m pulse.run triage --since 2026-09-01 --force   # re-label a range
.venv/bin/python -m pulse.run modqueue            # refresh who needs a reply
.venv/bin/python -m pulse.run pipeline            # all of the above
```

Spending is capped by `[budget] daily_usd_cap`; once reached, agent calls stop for the day and the run says so.

## Tests

```bash
.venv/bin/pytest
```
````

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (88 tests across 12 files).

- [ ] **Step 9: Commit**

```bash
git add pulse/pipeline.py pulse/run.py pulse.toml.example README.md tests/test_pipeline.py tests/test_cli.py
git commit -m "feat: pipeline wiring, CLI, sample config and README" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
