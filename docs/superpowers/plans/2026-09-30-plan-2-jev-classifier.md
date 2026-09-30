# Discord Pulse Plan 2: Jev First-Pass Classifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Label every message with Jev first (needs_reply probability, kind, sentiment), send only the messages that matter to the LLM, and list the mod queue by priority from the CLI.

**Architecture:** Jev is a closed-set classifier reached through OpenRouter's `/api/v1/systemone` endpoint. It sits behind a small `Classifier` protocol and is called through `LLMClient.classify`, which shares the existing budget gate, retry policy, lock and `agent_runs` logging. Triage becomes two stages: Stage A (Jev, per message, concurrent) writes confident low-stakes labels directly; Stage B (the existing batched LLM path) handles escalated messages and anything Jev failed on.

**Tech Stack:** Python 3.12, sqlite3, httpx (already installed as an SDK dependency; promoted to a direct dependency), pytest with `httpx.MockTransport`.

**Spec:** `docs/superpowers/specs/2026-09-29-discord-pulse-design.md` (section 14 is the binding design for this plan). Evidence: `docs/benchmarks/2026-09-30-triage-llm-vs-jev.md`.

**Plan series:** Plan 1 (core pipeline) done. Plan 2 (this) = Jev classifier. Plan 3 = stats, theme (with Jev assignment), digest, investigate agents. Plan 4 = FastAPI dashboard (Bugs view included) + seed-demo. Plan 5 = eval harness (with the Jev gate), bot adapter, bot pitch, launchd.

## Global Constraints

- Python `>=3.12`; run tests with `.venv/bin/pytest`; tests never touch the network (Jev is faked with `httpx.MockTransport` or `FakeClassifier`).
- All stored timestamps come from `pulse.models.to_iso` (fixed-width UTC `%Y-%m-%dT%H:%M:%S.%fZ`).
- Secrets only from env vars. The `jev` provider uses `OPENROUTER_API_KEY`.
- Agent modules never import a provider SDK or httpx; they call `LLMClient.complete` / `LLMClient.classify`.
- DB access from worker threads happens only inside `LLMClient` under its lock; triage writes use `with llm.db_lock, conn:`.
- Every commit message ends with exactly `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`, separated from the body by a blank line.
- Jev endpoint: `POST https://openrouter.ai/api/v1/systemone`, body `{"model", "state", "questions"}`, response `{"model", "answers": {name: {...}}, "usage": {"input_tokens", "output_tokens", "cost"}}`. Noul answers carry `noul` (probability of yes); choice answers carry `choice`, `confidence`, `probabilities`.
- Spec clarifications made by this plan (treat as spec): Jev's cost is the response's `usage.cost`; `[pricing."jev:<model>"] per_request` is an optional fallback used only when a response carries no cost (spec 14.3 said required). Jev-labelled triage rows use `prompt_version = "jev-q1"`. Classifier calls are logged in `agent_runs` with `agent = "classifier"`. When the budget cap is hit during Stage A, Stage B is skipped for that run; unfinished messages stay untriaged and are retried next run.

## Review Focus

1. A `pulse.db` created by Plan 1 (triage table without the new columns) must upgrade in place on the next `connect`, keeping every existing row (Task 2 `test_old_triage_table_is_migrated_in_place`).
2. Jev returning labels outside the known sets (unknown kind, sentiment "3", probability > 1, missing answer) must never be stored: it is `OutputInvalid`, retried once, then the message escalates to the LLM (Task 3 `test_unknown_kind_is_output_invalid_with_cost`, Task 5 `test_classifier_failure_escalates_to_llm`).
3. A staff message that Jev scores as an urgent bug must be stored neutral and never escalated or queued (Task 5 `test_staff_messages_are_overridden_and_never_escalated`).
4. Hitting the budget cap during Stage A must stop the run with no LLM calls and no partial rows (Task 5 `test_budget_hit_in_classifier_stage_stops_triage`).
5. With `[classifier]` absent or `enabled = false`, triage must behave exactly as Plan 1 (all existing `tests/test_triage.py` tests unchanged, plus Task 5 `test_disabled_classifier_uses_llm_only`).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `pulse/config.py` | `[classifier]` section, `jev` provider, `per_request` pricing | 1 |
| `pulse/db.py` | triage columns `needs_reply_p`, `kind_confidence`, `labeler`; in-place migration | 2 |
| `pulse/agents/classifier.py` | `Classifier` protocol, `ClassifierResult`, `JEV_QUESTIONS`, `parse_answers` | 3 |
| `pulse/agents/providers/jev_backend.py` | httpx client for `/systemone` | 3 |
| `pulse/pricing.py` | `request_cost` | 4 |
| `pulse/agents/llm.py` | `LLMClient.classify`, `config`, `has_classifier` | 4 |
| `tests/fakes.py` | `FakeClassifier`, `jev_result`, `classifier_config`, `make_llm(classifier=)` | 4 |
| `pulse/agents/triage.py` | two-stage triage | 5 |
| `pulse/modqueue.py` | `list_open` priority listing | 6 |
| `pulse/pipeline.py`, `pulse/run.py` | `format_queue`, `queue` command, Jev wiring, triage report line | 6, 7 |
| `pulse.toml.example`, `README.md`, `pyproject.toml` | docs and httpx dependency | 3, 7 |

---

### Task 1: Classifier config, `jev` provider, per-request pricing

**Files:**
- Modify: `pulse/config.py`
- Test: `tests/test_config.py` (append)

**Interfaces:**
- Consumes: `KINDS` from `pulse.models`.
- Produces:
  - `CLASSIFIER_PROVIDERS = ("jev",)`; `KEY_ENV["jev"] == "OPENROUTER_API_KEY"`
  - `ModelRef.parse(value: str, providers: tuple[str, ...] = PROVIDERS) -> ModelRef`
  - `Price(input: float, output: float, cache_read: float = 0.0, per_request: float = 0.0)`
  - `DEFAULT_ESCALATE_KINDS = ("bug", "docs", "feature_request", "praise")`
  - `@dataclass(frozen=True) ClassifierConfig(enabled: bool, model: ModelRef, needs_reply_threshold: float = 0.7, min_confidence: float = 0.6, escalate_kinds: tuple[str, ...] = DEFAULT_ESCALATE_KINDS)`
  - `Config.classifier: ClassifierConfig | None = None` (new last field)

- [ ] **Step 1: Write the failing tests** — append to `tests/test_config.py`:

```python
from pulse.config import ClassifierConfig, Price

CLASSIFIER = '''
[classifier]
enabled = true
model = "jev:jev-latest"
needs_reply_threshold = 0.75
min_confidence = 0.5
escalate_kinds = ["bug", "docs"]

[pricing."jev:jev-latest"]
per_request = 0.0000387
'''


def test_classifier_section_parsed(tmp_path):
    cfg = load_config(write(tmp_path, BASE + CLASSIFIER), env=ENV)
    assert cfg.classifier == ClassifierConfig(
        enabled=True, model=ModelRef("jev", "jev-latest"), needs_reply_threshold=0.75,
        min_confidence=0.5, escalate_kinds=("bug", "docs"),
    )
    assert cfg.pricing["jev:jev-latest"] == Price(0.0, 0.0, 0.0, per_request=0.0000387)


def test_classifier_absent_is_none(tmp_path):
    assert load_config(write(tmp_path, BASE), env=ENV).classifier is None


def test_classifier_defaults(tmp_path):
    cfg = load_config(write(tmp_path, BASE + '\n[classifier]\nenabled = true\n'), env=ENV)
    assert cfg.classifier.model == ModelRef("jev", "jev-latest")
    assert cfg.classifier.needs_reply_threshold == 0.7
    assert cfg.classifier.min_confidence == 0.6
    assert cfg.classifier.escalate_kinds == ("bug", "docs", "feature_request", "praise")


def test_enabled_classifier_requires_openrouter_key(tmp_path):
    text = BASE.replace('investigate = "openrouter:anthropic/claude-sonnet-5"', 'investigate = "anthropic:claude-opus-5-5"')
    env = dict(ENV)
    del env["OPENROUTER_API_KEY"]
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY.*classifier"):
        load_config(write(tmp_path, text + CLASSIFIER), env=env)


def test_disabled_classifier_needs_no_key(tmp_path):
    text = BASE.replace('investigate = "openrouter:anthropic/claude-sonnet-5"', 'investigate = "anthropic:claude-opus-5-5"')
    env = dict(ENV)
    del env["OPENROUTER_API_KEY"]
    cfg = load_config(write(tmp_path, text + CLASSIFIER.replace("enabled = true", "enabled = false")), env=env)
    assert cfg.classifier.enabled is False


def test_classifier_rejects_non_jev_provider(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, BASE + CLASSIFIER.replace('model = "jev:jev-latest"', 'model = "openai:gpt-x"')), env=ENV)


def test_classifier_rejects_unknown_kind(tmp_path):
    with pytest.raises(ConfigError, match="escalate_kinds"):
        load_config(write(tmp_path, BASE + CLASSIFIER.replace('["bug", "docs"]', '["bug", "rants"]')), env=ENV)


def test_classifier_rejects_threshold_out_of_range(tmp_path):
    with pytest.raises(ConfigError, match="needs_reply_threshold"):
        load_config(write(tmp_path, BASE + CLASSIFIER.replace("0.75", "1.5")), env=ENV)


def test_llm_agents_still_reject_jev_provider(tmp_path):
    with pytest.raises(ConfigError, match="provider"):
        load_config(write(tmp_path, BASE.replace('theme = "openai:gpt-x"', 'theme = "jev:jev-latest"')), env=ENV)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'ClassifierConfig'`

- [ ] **Step 3: Implement** — edit `pulse/config.py`:

Add the import and constants near the top (after `from typing import Any, Mapping`):

```python
from pulse.models import KINDS
```

Replace the `PROVIDERS`/`KEY_ENV` block with:

```python
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
```

Replace `ModelRef.parse` with:

```python
    @classmethod
    def parse(cls, value: str, providers: tuple[str, ...] = PROVIDERS) -> ModelRef:
        provider, sep, model = value.partition(":")
        if not sep or not model or provider not in providers:
            raise ConfigError(
                f"model must be 'provider:model' with provider in {providers}, got {value!r}"
            )
        return cls(provider, model)
```

Replace `Price` with:

```python
@dataclass(frozen=True)
class Price:
    """USD per million tokens, or per request for request-priced models (Jev)."""

    input: float
    output: float
    cache_read: float = 0.0
    per_request: float = 0.0
```

Add after `Launch`:

```python
@dataclass(frozen=True)
class ClassifierConfig:
    enabled: bool
    model: ModelRef
    needs_reply_threshold: float = 0.7
    min_confidence: float = 0.6
    escalate_kinds: tuple[str, ...] = DEFAULT_ESCALATE_KINDS
```

Add `classifier: ClassifierConfig | None = None` as the LAST field of `Config`.

Replace `_price` with:

```python
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
```

Add after `_launch`:

```python
def _unit_interval(name: str, value: Any) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError) as e:
        raise ConfigError(f"[classifier] {name} must be a number between 0 and 1") from e
    if not 0.0 <= v <= 1.0:
        raise ConfigError(f"[classifier] {name} must be between 0 and 1, got {v}")
    return v


def _classifier(raw: Any, env: Mapping[str, str]) -> ClassifierConfig | None:
    if not raw:
        return None
    model = ModelRef.parse(str(raw.get("model", "jev:jev-latest")), CLASSIFIER_PROVIDERS)
    kinds = tuple(str(k) for k in raw.get("escalate_kinds", DEFAULT_ESCALATE_KINDS))
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ConfigError(f"[classifier] escalate_kinds has unknown kinds {unknown}; allowed {list(KINDS)}")
    cfg = ClassifierConfig(
        enabled=bool(raw.get("enabled", False)),
        model=model,
        needs_reply_threshold=_unit_interval("needs_reply_threshold", raw.get("needs_reply_threshold", 0.7)),
        min_confidence=_unit_interval("min_confidence", raw.get("min_confidence", 0.6)),
        escalate_kinds=kinds,
    )
    if cfg.enabled and not env.get(KEY_ENV[model.provider]):
        raise ConfigError(f"{KEY_ENV[model.provider]} must be set because the classifier {model} is enabled")
    return cfg
```

In `load_config`, pass the new field in the `Config(...)` call:

```python
        classifier=_classifier(raw.get("classifier"), env),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: all pass (12 existing + 9 new = 21)

- [ ] **Step 5: Run the full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/config.py tests/test_config.py
git commit -m "feat: [classifier] config section with jev provider and per-request pricing" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Triage columns for the classifier, with in-place migration

**Files:**
- Modify: `pulse/db.py`
- Test: `tests/test_db.py` (append)

**Interfaces:**
- Produces: `triage` columns `needs_reply_p REAL` (nullable), `kind_confidence REAL` (nullable), `labeler TEXT NOT NULL DEFAULT 'llm'`; `connect()` upgrades older databases in place.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_db.py`:

```python
OLD_SCHEMA = """
CREATE TABLE messages (id TEXT PRIMARY KEY, guild_id TEXT NOT NULL, channel_id TEXT NOT NULL,
  channel_name TEXT NOT NULL DEFAULT '', thread_id TEXT, author_id TEXT NOT NULL, author_name TEXT NOT NULL,
  author_avatar_url TEXT, is_team INTEGER NOT NULL DEFAULT 0, is_bot INTEGER NOT NULL DEFAULT 0,
  content TEXT NOT NULL, created_at TEXT NOT NULL, edited_at TEXT, reply_to_id TEXT, source TEXT NOT NULL);
CREATE TABLE triage (message_id TEXT PRIMARY KEY, sentiment INTEGER NOT NULL, confidence REAL NOT NULL,
  kind TEXT NOT NULL, topics TEXT NOT NULL, needs_reply INTEGER NOT NULL, prompt_version TEXT NOT NULL,
  run_id INTEGER, created_at TEXT NOT NULL);
INSERT INTO messages (id, guild_id, channel_id, author_id, author_name, content, created_at, source)
  VALUES ('m', 'g', 'c', 'a', 'n', 'hi', 'x', 'file');
INSERT INTO triage VALUES ('m', -1, 0.9, 'bug', '[]', 1, 'triage-v2', NULL, 'x');
"""


def triage_columns(conn):
    return {r["name"] for r in conn.execute("PRAGMA table_info(triage)")}


def test_fresh_db_has_classifier_columns():
    assert {"needs_reply_p", "kind_confidence", "labeler"} <= triage_columns(connect(":memory:"))


def test_old_triage_table_is_migrated_in_place(tmp_path):
    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.commit()
    raw.close()

    conn = connect(path)

    assert {"needs_reply_p", "kind_confidence", "labeler"} <= triage_columns(conn)
    row = conn.execute("SELECT * FROM triage WHERE message_id = 'm'").fetchone()
    assert (row["sentiment"], row["kind"], row["labeler"], row["needs_reply_p"], row["kind_confidence"]) == (
        -1, "bug", "llm", None, None
    )


def test_migration_is_idempotent(tmp_path):
    path = tmp_path / "p.db"
    connect(path).close()
    connect(path).close()
    assert "labeler" in triage_columns(connect(path))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_db.py -v`
Expected: FAIL (`test_fresh_db_has_classifier_columns` assertion, and the migration test)

- [ ] **Step 3: Implement** — edit `pulse/db.py`:

In `SCHEMA`, replace the `triage` table definition with:

```sql
CREATE TABLE IF NOT EXISTS triage (
    message_id TEXT PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    sentiment INTEGER NOT NULL CHECK (sentiment BETWEEN -2 AND 2),
    confidence REAL NOT NULL,
    kind TEXT NOT NULL,
    topics TEXT NOT NULL,
    needs_reply INTEGER NOT NULL,
    prompt_version TEXT NOT NULL,
    run_id INTEGER REFERENCES agent_runs(id),
    created_at TEXT NOT NULL,
    needs_reply_p REAL,
    kind_confidence REAL,
    labeler TEXT NOT NULL DEFAULT 'llm'
);
```

Add above `connect`:

```python
# Columns added after Plan 1. CREATE TABLE IF NOT EXISTS leaves an existing table
# untouched, so databases created earlier get them via ALTER TABLE.
_TRIAGE_ADDED_COLUMNS = (
    ("needs_reply_p", "REAL"),
    ("kind_confidence", "REAL"),
    ("labeler", "TEXT NOT NULL DEFAULT 'llm'"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(triage)")}
    with conn:
        for name, decl in _TRIAGE_ADDED_COLUMNS:
            if name not in have:
                conn.execute(f"ALTER TABLE triage ADD COLUMN {name} {decl}")
```

In `connect`, call `_migrate(conn)` right after `conn.executescript(SCHEMA)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_db.py -v` — Expected: all pass (4 existing + 3 new).

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/db.py tests/test_db.py
git commit -m "feat: triage columns for classifier labels with in-place migration" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Classifier contract and the Jev backend

**Files:**
- Create: `pulse/agents/classifier.py`, `pulse/agents/providers/jev_backend.py`, `tests/fixtures/jev_response.json`
- Modify: `pyproject.toml` (add `httpx>=0.27` to `dependencies`)
- Test: `tests/test_jev_backend.py`

**Interfaces:**
- Consumes: `KINDS` (models); `OutputInvalid`, `ProviderError`, `TransientError` (agents/base).
- Produces:
  - `pulse/agents/classifier.py`: `QUESTIONS_VERSION = "jev-q1"`, `SENTIMENT_LABELS = ("-2", "-1", "0", "1", "2")`, `JEV_QUESTIONS: dict` (wire-format questions named `needs_reply`, `kind`, `sentiment`), `@dataclass(frozen=True) ClassifierResult(needs_reply_p: float, kind: str, kind_confidence: float, sentiment: int, sentiment_confidence: float, reported_cost: float | None = None)`, `class Classifier(Protocol): classify(self, model: str, state: dict) -> ClassifierResult`, `parse_answers(data: dict) -> ClassifierResult` (raises `ValueError`/`KeyError`/`TypeError` on anything unexpected).
  - `pulse/agents/providers/jev_backend.py`: `SYSTEMONE_URL = "https://openrouter.ai/api/v1/systemone"`, `class JevBackend(client: httpx.Client | None = None, *, api_key: str | None = None, url: str = SYSTEMONE_URL, timeout: float = 30.0)` implementing `Classifier`.

- [ ] **Step 1: Add httpx as a direct dependency**

In `pyproject.toml`, change `dependencies` to:

```toml
dependencies = [
    "anthropic>=0.40",
    "openai>=1.50",
    "jsonschema>=4.21",
    "httpx>=0.27",
]
```

Run: `uv pip install -e '.[dev]'` — Expected: succeeds (httpx is already present).

- [ ] **Step 2: Create the recorded-response fixture** `tests/fixtures/jev_response.json` (shape recorded from a live call on 2026-09-30; the `sentiment` answer follows the same choice shape):

```json
{
  "model": "typesafe/jev-1.13-20260917",
  "answers": {
    "needs_reply": {"type": "noul", "noul": 0.93},
    "kind": {
      "type": "choice",
      "choice": "bug",
      "probabilities": {"bug": 0.95, "docs": 0.02, "question": 0.01, "feature_request": 0.0, "praise": 0.0, "other": 0.02},
      "confidence": 0.94
    },
    "sentiment": {
      "type": "choice",
      "choice": "-1",
      "probabilities": {"-2": 0.1, "-1": 0.85, "0": 0.05, "1": 0.0, "2": 0.0},
      "confidence": 0.8
    }
  },
  "usage": {"input_tokens": 610, "output_tokens": 80, "cost": 1.7724e-05},
  "id": "gen-test",
  "provider": "TypeSafe"
}
```

- [ ] **Step 3: Write the failing tests** — `tests/test_jev_backend.py`:

```python
import json
from pathlib import Path

import httpx
import pytest

from pulse.agents.base import OutputInvalid, ProviderError, TransientError
from pulse.agents.classifier import JEV_QUESTIONS, ClassifierResult
from pulse.agents.providers.jev_backend import SYSTEMONE_URL, JevBackend

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "jev_response.json").read_text())
STATE = {"message_id": "m1", "author": "alice", "is_team": False, "channel": "help",
         "content": "Install fails on M1", "reply_to": None, "context": []}


def backend_with(handler):
    seen = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return JevBackend(client=httpx.Client(transport=httpx.MockTransport(wrapped)), api_key="k"), seen


def test_posts_questions_and_parses_answers():
    backend, seen = backend_with(lambda r: httpx.Response(200, json=FIXTURE))
    result = backend.classify("jev-latest", STATE)
    assert result == ClassifierResult(
        needs_reply_p=0.93, kind="bug", kind_confidence=0.94, sentiment=-1,
        sentiment_confidence=0.8, reported_cost=pytest.approx(1.7724e-05),
    )
    [req] = seen
    assert str(req.url) == SYSTEMONE_URL
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body == {"model": "jev-latest", "state": STATE, "questions": JEV_QUESTIONS}
    assert set(body["questions"]) == {"needs_reply", "kind", "sentiment"}


def test_questions_cover_every_kind_and_sentiment():
    assert set(JEV_QUESTIONS["kind"]["criteria"]) == {"bug", "question", "feature_request", "docs", "praise", "other"}
    assert set(JEV_QUESTIONS["sentiment"]["criteria"]) == {"-2", "-1", "0", "1", "2"}
    assert JEV_QUESTIONS["needs_reply"]["type"] == "noul"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_rate_limit_and_server_errors_are_transient(status):
    backend, _ = backend_with(lambda r: httpx.Response(status, text="busy"))
    with pytest.raises(TransientError):
        backend.classify("jev-latest", STATE)


def test_connection_error_is_transient():
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    backend, _ = backend_with(boom)
    with pytest.raises(TransientError):
        backend.classify("jev-latest", STATE)


def test_bad_request_is_provider_error():
    backend, _ = backend_with(lambda r: httpx.Response(400, json={"error": {"message": "invalid_union"}}))
    with pytest.raises(ProviderError, match="400"):
        backend.classify("jev-latest", STATE)


def test_non_json_body_is_output_invalid():
    backend, _ = backend_with(lambda r: httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(OutputInvalid):
        backend.classify("jev-latest", STATE)


def test_unknown_kind_is_output_invalid_with_cost():
    bad = json.loads(json.dumps(FIXTURE))
    bad["answers"]["kind"]["choice"] = "rant"
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid) as exc:
        backend.classify("jev-latest", STATE)
    assert exc.value.reported_cost == pytest.approx(1.7724e-05)


@pytest.mark.parametrize("path,value", [
    (("sentiment", "choice"), "3"),
    (("needs_reply", "noul"), 1.4),
])
def test_out_of_range_answers_are_output_invalid(path, value):
    bad = json.loads(json.dumps(FIXTURE))
    bad["answers"][path[0]][path[1]] = value
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid):
        backend.classify("jev-latest", STATE)


def test_missing_answer_is_output_invalid():
    bad = json.loads(json.dumps(FIXTURE))
    del bad["answers"]["sentiment"]
    backend, _ = backend_with(lambda r: httpx.Response(200, json=bad))
    with pytest.raises(OutputInvalid):
        backend.classify("jev-latest", STATE)


def test_missing_cost_leaves_reported_cost_none():
    no_cost = json.loads(json.dumps(FIXTURE))
    del no_cost["usage"]["cost"]
    backend, _ = backend_with(lambda r: httpx.Response(200, json=no_cost))
    assert backend.classify("jev-latest", STATE).reported_cost is None
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_jev_backend.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.agents.classifier'`

- [ ] **Step 5: Implement** `pulse/agents/classifier.py`:

```python
"""Closed-set first-pass classifier (Jev): questions, result type, answer parsing.

Jev only picks from the labels offered, so it covers the decisions triage makes
(needs a reply, kind, sentiment); topics and prose stay with the LLM.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pulse.models import KINDS

QUESTIONS_VERSION = "jev-q1"
SENTIMENT_LABELS = ("-2", "-1", "0", "1", "2")

# Wire format for /systemone questions. Wording matches the 2026-09-30 benchmark.
JEV_QUESTIONS = {
    "needs_reply": {
        "type": "noul",
        "instructions": "Would a developer product's community team want a staff member to respond to this Discord message?",
        "criteria": {
            "true": "A non-staff user asks a question about using the product, or reports a bug, outage, error, "
                    "broken or wrong docs, or data loss (even without a question mark), or says they have the same "
                    "problem as someone else.",
            "false": "The author is staff (is_team true); or the message is aimed at other community members "
                     "(social plans, polls, 'anyone else using X?', meetups); or it is thanks, praise, chit-chat, "
                     "a feature request with nothing blocking the user, an answer, or the user says they solved it.",
        },
    },
    "kind": {
        "type": "choice",
        "instructions": "What kind of message is this, about the developer product?",
        "criteria": {
            "bug": "Something is broken or behaves wrongly.",
            "docs": "The documentation is missing, wrong, outdated, or confusing, including a how-to question caused by the docs skipping a step.",
            "question": "Asking how to do something with the product, when the docs are not the stated problem.",
            "feature_request": "Asking for something that does not exist yet.",
            "praise": "Compliments or enthusiasm about the product.",
            "other": "Everything else: staff announcements, short thanks, social chat.",
        },
    },
    "sentiment": {
        "type": "choice",
        "instructions": "How does the author feel about their experience with the product? Staff announcements and "
                        "answers are neutral. Developer slang like 'this is sick' is positive; sarcasm like "
                        "'love how it breaks every update' is negative.",
        "criteria": {
            "-2": "Angry, blocked, or reporting real damage (outage, data loss, considering switching).",
            "-1": "Frustrated, confused, or reporting something broken.",
            "0": "Neutral: questions, information, chit-chat.",
            "1": "Positive, thanks.",
            "2": "Enthusiastic praise.",
        },
    },
}


@dataclass(frozen=True)
class ClassifierResult:
    needs_reply_p: float
    kind: str
    kind_confidence: float
    sentiment: int
    sentiment_confidence: float
    reported_cost: float | None = None


class Classifier(Protocol):
    def classify(self, model: str, state: dict) -> ClassifierResult: ...


def parse_answers(data: dict) -> ClassifierResult:
    """Parse a /systemone response body. Raises on anything outside the offered labels."""
    answers = data["answers"]
    p = float(answers["needs_reply"]["noul"])
    kind = str(answers["kind"]["choice"])
    sentiment = str(answers["sentiment"]["choice"])
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"needs_reply probability out of range: {p}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if sentiment not in SENTIMENT_LABELS:
        raise ValueError(f"unknown sentiment {sentiment!r}")
    return ClassifierResult(
        needs_reply_p=p,
        kind=kind,
        kind_confidence=float(answers["kind"]["confidence"]),
        sentiment=int(sentiment),
        sentiment_confidence=float(answers["sentiment"]["confidence"]),
    )
```

- [ ] **Step 6: Implement** `pulse/agents/providers/jev_backend.py`:

```python
"""Jev through OpenRouter's /systemone endpoint (TypeSafe closed-set classifier)."""
from __future__ import annotations

import os
from dataclasses import replace

import httpx

from pulse.agents.base import OutputInvalid, ProviderError, TransientError
from pulse.agents.classifier import JEV_QUESTIONS, ClassifierResult, parse_answers

SYSTEMONE_URL = "https://openrouter.ai/api/v1/systemone"


class JevBackend:
    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        api_key: str | None = None,
        url: str = SYSTEMONE_URL,
        timeout: float = 30.0,
    ):
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._api_key = api_key if api_key is not None else os.environ.get("OPENROUTER_API_KEY", "")
        self._url = url

    def classify(self, model: str, state: dict) -> ClassifierResult:
        try:
            resp = self._client.post(
                self._url,
                json={"model": model, "state": state, "questions": JEV_QUESTIONS},
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TransportError as e:
            raise TransientError(str(e)) from e
        if resp.status_code == 429 or resp.status_code >= 500:
            raise TransientError(f"{resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"{resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as e:
            raise OutputInvalid(f"not JSON: {e}") from e
        cost = (data.get("usage") or {}).get("cost") if isinstance(data, dict) else None
        reported = float(cost) if cost is not None else None
        try:
            result = parse_answers(data)
        except (KeyError, TypeError, ValueError) as e:
            raise OutputInvalid(f"unexpected answers: {e!r}", reported_cost=reported) from e
        return replace(result, reported_cost=reported)
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_jev_backend.py -v` — Expected: 13 passed.

- [ ] **Step 8: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pyproject.toml pulse/agents/classifier.py pulse/agents/providers/jev_backend.py tests/fixtures/jev_response.json tests/test_jev_backend.py
git commit -m "feat: Jev classifier contract and OpenRouter systemone backend" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `LLMClient.classify` with shared budget, retries and run logging

**Files:**
- Modify: `pulse/pricing.py`, `pulse/agents/llm.py`, `tests/fakes.py`
- Test: `tests/test_llm_classify.py`

**Interfaces:**
- Consumes: `ClassifierConfig`, `Price`, `ModelRef` (Task 1); `Classifier`, `ClassifierResult` (Task 3).
- Produces:
  - `pulse/pricing.py`: `request_cost(price: Price | None, reported: float | None) -> float`
  - `pulse/agents/llm.py`: `@dataclass(frozen=True) ClassifyResponse(result: ClassifierResult, run_id: int)`; `LLMClient.__init__(..., classifier: Classifier | None = None)`; properties `config -> Config`, `has_classifier -> bool`; `classify(state: dict) -> ClassifyResponse` (raises `BudgetExceeded`, `LLMError`, or `RuntimeError` if no classifier is configured). Logged in `agent_runs` as agent `classifier`, model `str(config.classifier.model)`.
  - `tests/fakes.py`: `FakeClassifier(responses=None, handler=None)` with `.calls`, `jev_result(...)`, `classifier_config(**overrides)`, `make_llm(conn, config, backend, *, sleeps=None, classifier=None)`.

- [ ] **Step 1: Extend `tests/fakes.py`** — add below the existing `FakeBackend`, and replace `make_llm`:

```python
from pulse.agents.classifier import ClassifierResult  # noqa: E402
from pulse.config import ClassifierConfig  # noqa: E402


def jev_result(p=0.1, kind="other", sentiment=0, kind_conf=0.9, sent_conf=0.9, cost=0.00002) -> ClassifierResult:
    return ClassifierResult(
        needs_reply_p=p, kind=kind, kind_confidence=kind_conf, sentiment=sentiment,
        sentiment_confidence=sent_conf, reported_cost=cost,
    )


def classifier_config(**overrides) -> ClassifierConfig:
    base = dict(enabled=True, model=ModelRef("jev", "jev-latest"))
    base.update(overrides)
    return ClassifierConfig(**base)


class FakeClassifier:
    """Like FakeBackend: queued items first, then handler(state). Exceptions are raised."""

    def __init__(self, responses=None, handler=None):
        self.responses = list(responses or [])
        self.handler = handler
        self.calls: list[dict] = []

    def classify(self, model, state):
        self.calls.append({"model": model, "state": state})
        item = self.responses.pop(0) if self.responses else self.handler
        if item is None:
            raise AssertionError("FakeClassifier has no response queued")
        if callable(item) and not isinstance(item, ClassifierResult):
            item = item(state)
        if isinstance(item, Exception):
            raise item
        return item


def make_llm(conn, config, backend, *, sleeps=None, classifier=None) -> LLMClient:
    sleeps = [] if sleeps is None else sleeps
    return LLMClient(
        conn, config, {"anthropic": backend}, now=lambda: FIXED_NOW, sleep=sleeps.append, classifier=classifier
    )
```

- [ ] **Step 2: Write the failing tests** — `tests/test_llm_classify.py`:

```python
import pytest

from pulse.agents.base import BudgetExceeded, LLMError, OutputInvalid, ProviderError, TransientError
from pulse.config import Price
from pulse.db import connect
from tests.fakes import FakeBackend, FakeClassifier, classifier_config, jev_result, make_config, make_llm

STATE = {"message_id": "m1", "content": "hi"}


def runs(conn):
    return conn.execute("SELECT * FROM agent_runs ORDER BY id").fetchall()


def setup(responses, **config_overrides):
    conn = connect(":memory:")
    fc = FakeClassifier(responses)
    config = make_config(classifier=classifier_config(), **config_overrides)
    sleeps = []
    return conn, fc, make_llm(conn, config, FakeBackend(), sleeps=sleeps, classifier=fc), sleeps


def test_classify_returns_result_and_logs_reported_cost():
    conn, fc, llm, _ = setup([jev_result(kind="bug", cost=0.00002)])
    resp = llm.classify(STATE)
    assert resp.result.kind == "bug"
    [run] = runs(conn)
    assert resp.run_id == run["id"]
    assert (run["agent"], run["model"], run["status"]) == ("classifier", "jev:jev-latest", "ok")
    assert run["cost_usd"] == pytest.approx(0.00002)
    assert fc.calls == [{"model": "jev-latest", "state": STATE}]


def test_per_request_price_used_when_no_reported_cost():
    pricing = dict(make_config().pricing, **{"jev:jev-latest": Price(0.0, 0.0, per_request=0.00004)})
    conn, _, llm, _ = setup([jev_result(cost=None)], pricing=pricing)
    llm.classify(STATE)
    assert runs(conn)[0]["cost_usd"] == pytest.approx(0.00004)


def test_classify_is_blocked_by_budget_cap():
    conn, fc, llm, _ = setup([jev_result()], daily_usd_cap=0.0)
    with pytest.raises(BudgetExceeded):
        llm.classify(STATE)
    assert fc.calls == []
    assert runs(conn)[0]["status"] == "skipped_budget"


def test_classify_retries_transient_errors():
    conn, fc, llm, sleeps = setup([TransientError("429"), jev_result()])
    llm.classify(STATE)
    assert sleeps == [1]
    assert len(fc.calls) == 2


def test_invalid_output_twice_fails_and_bills_both():
    bad = OutputInvalid("unknown kind", reported_cost=0.00001)
    conn, fc, llm, _ = setup([bad, bad])
    with pytest.raises(LLMError, match="classifier"):
        llm.classify(STATE)
    [run] = runs(conn)
    assert run["status"] == "failed"
    assert run["cost_usd"] == pytest.approx(0.00002)


def test_provider_error_fails_without_retry():
    conn, fc, llm, _ = setup([ProviderError("401")])
    with pytest.raises(LLMError):
        llm.classify(STATE)
    assert len(fc.calls) == 1


def test_classify_without_classifier_raises():
    llm = make_llm(connect(":memory:"), make_config(), FakeBackend())
    assert llm.has_classifier is False
    with pytest.raises(RuntimeError, match="classifier"):
        llm.classify(STATE)


def test_config_property_exposes_config():
    config = make_config(classifier=classifier_config())
    llm = make_llm(connect(":memory:"), config, FakeBackend(), classifier=FakeClassifier())
    assert llm.config is config
    assert llm.has_classifier is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_llm_classify.py -v`
Expected: FAIL (`TypeError: LLMClient.__init__() got an unexpected keyword argument 'classifier'`)

- [ ] **Step 4: Implement**

`pulse/pricing.py` — append:

```python
def request_cost(price: Price | None, reported: float | None) -> float:
    """Cost of one request to a request-priced model (Jev)."""
    if reported is not None:
        return reported
    return price.per_request if price is not None else 0.0
```

`pulse/agents/llm.py`:

Update imports:

```python
from pulse.agents.classifier import Classifier, ClassifierResult
from pulse.pricing import cost_usd, request_cost
```

Add after `LLMResponse`:

```python
@dataclass(frozen=True)
class ClassifyResponse:
    result: ClassifierResult
    run_id: int
```

Change `__init__` to accept and store the classifier (add the keyword after `sleep`):

```python
        sleep: Callable[[float], None] = time.sleep,
        classifier: Classifier | None = None,
    ):
        ...
        self._classifier = classifier
```

Add properties after `db_lock`:

```python
    @property
    def config(self) -> Config:
        return self._config

    @property
    def has_classifier(self) -> bool:
        return self._classifier is not None and self._config.classifier is not None
```

Replace `_call` with a generic backoff helper plus the existing call:

```python
    def _with_backoff(self, fn: Callable[[], Any]) -> Any:
        for attempt in range(MAX_TRANSIENT_ATTEMPTS):
            try:
                return fn()
            except TransientError:
                if attempt == MAX_TRANSIENT_ATTEMPTS - 1:
                    raise
                self._sleep(2**attempt)
        raise AssertionError("unreachable")

    def _call(self, backend: Backend, model: str, system: str, user: str, schema: dict, schema_name: str) -> BackendResult:
        return self._with_backoff(lambda: backend.complete(model, system, user, schema, schema_name))
```

Add `classify` after `complete`:

```python
    def classify(self, state: dict) -> ClassifyResponse:
        """Ask the configured classifier (Jev) about one message state.

        Same contract as complete(): budget gate, transient backoff, one retry on
        invalid output, exactly one agent_runs row (agent "classifier").
        """
        cfg = self._config.classifier
        if cfg is None or self._classifier is None:
            raise RuntimeError("classifier not configured")
        ref = cfg.model
        price = self._config.pricing.get(str(ref))
        started = self._now()
        if self.spent_today() >= self._config.daily_usd_cap:
            self._record("classifier", ref, started, "skipped_budget", "daily budget cap reached", _Usage())
            raise BudgetExceeded(f"daily budget cap ${self._config.daily_usd_cap:.2f} reached")

        usage = _Usage()
        error = "no attempt made"
        for _ in range(MAX_VALIDATION_ATTEMPTS):
            try:
                result = self._with_backoff(lambda: self._classifier.classify(ref.model, state))
            except OutputInvalid as e:
                usage.cost_usd += request_cost(price, e.reported_cost)
                error = f"invalid output: {e}"
                continue
            except (TransientError, ProviderError) as e:
                error = f"provider error: {e}"
                break
            usage.cost_usd += request_cost(price, result.reported_cost)
            run_id = self._record("classifier", ref, started, "ok", None, usage)
            return ClassifyResponse(result, run_id)

        self._record("classifier", ref, started, "failed", error, usage)
        raise LLMError(f"classifier: {error}")
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_llm_classify.py tests/test_llm.py -v` — Expected: all pass (8 new, 12 existing).

- [ ] **Step 6: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/pricing.py pulse/agents/llm.py tests/fakes.py tests/test_llm_classify.py
git commit -m "feat: LLMClient.classify with shared budget gate, retries and run logging" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Two-stage triage (Jev first, LLM for what matters)

**Files:**
- Modify: `pulse/agents/triage.py`
- Test: `tests/test_triage_classifier.py`

**Interfaces:**
- Consumes: `LLMClient.classify`, `.config`, `.has_classifier`, `.db_lock` (Task 4); `ClassifierResult`, `QUESTIONS_VERSION` (Task 3); `ClassifierConfig` (Task 1); fakes from Task 4.
- Produces:
  - `TriageStats` gains `jev_labeled: int = 0`, `escalated: int = 0`, `classifier_failed: int = 0`.
  - `run_triage(conn, llm, *, batch_size=BATCH_SIZE, concurrency=4, classify_concurrency=8, since=None, force=False) -> TriageStats`
  - `needs_escalation(result: ClassifierResult, cfg: ClassifierConfig) -> bool`
  - Rows written by Jev: `labeler='jev'`, `topics='[]'`, `confidence = sentiment_confidence`, `needs_reply = int(needs_reply_p >= threshold)`, `prompt_version = "jev-q1"`, plus `needs_reply_p`, `kind_confidence`. Rows written by the LLM: `labeler='llm'`, `needs_reply_p` = Jev's probability when Jev answered, else NULL; `kind_confidence` NULL.

Escalation (spec 14.2): sentiment < 0, or `needs_reply_p >= needs_reply_threshold`, or kind in `escalate_kinds`, or `min(kind_confidence, sentiment_confidence) < min_confidence`. Staff messages (`is_team`) are overridden to sentiment 0, kind `other`, `needs_reply_p` 0.0 and are never escalated. A message Jev failed on escalates. A budget stop during Stage A skips Stage B.

- [ ] **Step 1: Write the failing tests** — `tests/test_triage_classifier.py`:

```python
import json

from pulse.agents.base import BackendResult, ProviderError
from pulse.agents.classifier import QUESTIONS_VERSION
from pulse.agents.triage import run_triage
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import (
    FakeBackend, FakeClassifier, classifier_config, jev_result, make_config, make_llm, msg,
)

TEAM = frozenset({"t1"})


def llm_echo(user):
    items = json.loads(user)["messages"]
    return BackendResult({"results": [
        {"message_id": m["message_id"], "sentiment": -1, "confidence": 0.8, "kind": "bug",
         "topics": ["install"], "needs_reply": True}
        for m in items
    ]}, 100, 20)


def jev_by_content(state):
    text = state["content"]
    if "broken" in text:
        return jev_result(p=0.9, kind="bug", sentiment=-1)
    if "love" in text:
        return jev_result(p=0.05, kind="praise", sentiment=2)
    if "hmm" in text:
        return jev_result(p=0.2, kind="other", sentiment=0, kind_conf=0.4)
    if "known issue" in text:
        return jev_result(p=0.95, kind="bug", sentiment=-2)
    return jev_result(p=0.1, kind="other", sentiment=0)


def setup(messages, *, classifier=None, enabled=True, cap=5.0):
    conn = connect(":memory:")
    upsert_messages(conn, messages, TEAM)
    backend = FakeBackend(handler=llm_echo)
    fc = classifier or FakeClassifier(handler=jev_by_content)
    config = make_config(daily_usd_cap=cap, classifier=classifier_config(enabled=enabled))
    return conn, backend, fc, make_llm(conn, config, backend, classifier=fc)


def rows(conn):
    return {r["message_id"]: r for r in conn.execute("SELECT * FROM triage")}


def llm_ids(backend):
    return {m["message_id"] for c in backend.calls for m in json.loads(c["user"])["messages"]}


def test_confident_neutral_message_is_labeled_by_jev_only():
    conn, backend, fc, llm = setup([msg("m1", "good morning all")])
    stats = run_triage(conn, llm)
    r = rows(conn)["m1"]
    assert (r["labeler"], r["kind"], r["sentiment"], r["needs_reply"], json.loads(r["topics"])) == (
        "jev", "other", 0, 0, []
    )
    assert (r["needs_reply_p"], r["kind_confidence"], r["prompt_version"]) == (0.1, 0.9, QUESTIONS_VERSION)
    assert backend.calls == []
    assert (stats.triaged, stats.jev_labeled, stats.escalated) == (1, 1, 0)


def test_negative_message_escalates_to_llm_and_keeps_jev_probability():
    conn, backend, fc, llm = setup([msg("m1", "it's broken again"), msg("m2", "good morning", minutes=1)])
    stats = run_triage(conn, llm)
    assert llm_ids(backend) == {"m1"}
    r = rows(conn)
    assert (r["m1"]["labeler"], json.loads(r["m1"]["topics"]), r["m1"]["needs_reply_p"]) == ("llm", ["install"], 0.9)
    assert r["m2"]["labeler"] == "jev"
    assert (stats.triaged, stats.jev_labeled, stats.escalated) == (2, 1, 1)


def test_escalate_kinds_and_low_confidence_go_to_llm():
    conn, backend, fc, llm = setup([msg("p", "love the new cli"), msg("h", "hmm ok", minutes=1)])
    run_triage(conn, llm)
    assert llm_ids(backend) == {"p", "h"}


def test_staff_messages_are_overridden_and_never_escalated():
    conn, backend, fc, llm = setup([msg("s1", "known issue, fix ships today", author_id="t1")])
    run_triage(conn, llm)
    r = rows(conn)["s1"]
    assert (r["labeler"], r["sentiment"], r["kind"], r["needs_reply"], r["needs_reply_p"]) == ("jev", 0, "other", 0, 0.0)
    assert backend.calls == []


def test_classifier_failure_escalates_to_llm():
    fc = FakeClassifier(handler=lambda state: ProviderError("400"))
    conn, backend, fc, llm = setup([msg("m1", "good morning")], classifier=fc)
    stats = run_triage(conn, llm)
    assert llm_ids(backend) == {"m1"}
    assert stats.classifier_failed == 1
    r = rows(conn)["m1"]
    assert (r["labeler"], r["needs_reply_p"]) == ("llm", None)


def test_budget_hit_in_classifier_stage_stops_triage():
    conn, backend, fc, llm = setup([msg("m1", "it's broken"), msg("m2", "hi", minutes=1)], cap=0.0)
    stats = run_triage(conn, llm)
    assert rows(conn) == {}
    assert backend.calls == [] and fc.calls == []
    assert stats.skipped_budget_batches == 1
    assert stats.triaged == 0


def test_disabled_classifier_uses_llm_only():
    conn, backend, fc, llm = setup([msg("m1", "good morning")], enabled=False)
    run_triage(conn, llm)
    assert fc.calls == []
    assert llm_ids(backend) == {"m1"}
    assert rows(conn)["m1"]["labeler"] == "llm"


def test_escalated_messages_are_batched_newest_first():
    conn, backend, fc, llm = setup(
        [msg("old", "broken 1", minutes=0), msg("mid", "broken 2", minutes=1), msg("new", "broken 3", minutes=2)]
    )
    run_triage(conn, llm, batch_size=1, concurrency=1)
    first = json.loads(backend.calls[0]["user"])["messages"][0]["message_id"]
    assert first == "new"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_triage_classifier.py -v`
Expected: FAIL (Jev path not implemented; rows are labeled `llm` or `labeler` missing from stats)

- [ ] **Step 3: Implement** — edit `pulse/agents/triage.py`.

Update the module docstring:

```python
"""Triage: label every message with sentiment, kind, topics, needs_reply.

With the classifier enabled, Stage A asks Jev about every message and stores
confident, low-stakes labels directly; Stage B sends the rest to the batched
LLM path. Worker threads only call models; the calling thread writes each
result as it arrives, under the LLM client's DB lock.
"""
```

Update imports:

```python
from dataclasses import dataclass, replace

from pulse.agents.classifier import QUESTIONS_VERSION, ClassifierResult
from pulse.agents.llm import ClassifyResponse, LLMClient, LLMResponse
from pulse.config import ClassifierConfig
```

Replace `TriageStats` with:

```python
@dataclass
class TriageStats:
    triaged: int = 0
    failed_batches: int = 0
    skipped_budget_batches: int = 0
    jev_labeled: int = 0
    escalated: int = 0
    classifier_failed: int = 0
```

Add the insert statement and helpers above `run_triage`:

```python
_INSERT = (
    "INSERT OR REPLACE INTO triage (message_id, sentiment, confidence, kind, topics, needs_reply,"
    " prompt_version, run_id, created_at, needs_reply_p, kind_confidence, labeler)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def needs_escalation(result: ClassifierResult, cfg: ClassifierConfig) -> bool:
    return (
        result.sentiment < 0
        or result.needs_reply_p >= cfg.needs_reply_threshold
        or result.kind in cfg.escalate_kinds
        or min(result.kind_confidence, result.sentiment_confidence) < cfg.min_confidence
    )


def _staff_override(result: ClassifierResult) -> ClassifierResult:
    return replace(result, sentiment=0, kind="other", needs_reply_p=0.0)


def _classify_stage(
    conn: sqlite3.Connection,
    llm: LLMClient,
    rows: list[sqlite3.Row],
    cfg: ClassifierConfig,
    concurrency: int,
    stats: TriageStats,
    stop: threading.Event,
) -> tuple[list[sqlite3.Row], dict[str, float]]:
    """Stage A. Returns the rows to send to the LLM (newest first) and Jev's
    needs_reply probability per message id."""
    states = json.loads(build_batch_input(conn, rows))["messages"]
    by_id = {r["id"]: r for r in rows}

    def call(state: dict) -> tuple[str, ClassifyResponse | None, str | None]:
        mid = state["message_id"]
        if stop.is_set():
            return mid, None, "budget"
        try:
            return mid, llm.classify(state), None
        except BudgetExceeded:
            stop.set()
            return mid, None, "budget"
        except LLMError:
            return mid, None, "failed"

    escalate: list[sqlite3.Row] = []
    p_by_id: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(call, s) for s in states]
        for fut in as_completed(futures):
            mid, resp, error = fut.result()
            row = by_id[mid]
            if error == "budget":
                continue
            if error == "failed":
                stats.classifier_failed += 1
                escalate.append(row)
                continue
            result = _staff_override(resp.result) if row["is_team"] else resp.result
            p_by_id[mid] = result.needs_reply_p
            if not row["is_team"] and needs_escalation(result, cfg):
                escalate.append(row)
                continue
            now = to_iso(datetime.now(timezone.utc))
            with llm.db_lock, conn:
                conn.execute(_INSERT, (
                    mid, result.sentiment, result.sentiment_confidence, result.kind, "[]",
                    int(result.needs_reply_p >= cfg.needs_reply_threshold), QUESTIONS_VERSION,
                    resp.run_id, now, result.needs_reply_p, result.kind_confidence, "jev",
                ))
            stats.jev_labeled += 1
            stats.triaged += 1
    escalate.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
    stats.escalated = len(escalate)
    return escalate, p_by_id
```

Replace `run_triage` with:

```python
def run_triage(
    conn: sqlite3.Connection,
    llm: LLMClient,
    *,
    batch_size: int = BATCH_SIZE,
    concurrency: int = 4,
    classify_concurrency: int = 8,
    since: datetime | None = None,
    force: bool = False,
) -> TriageStats:
    rows = select_untriaged(conn, since, force=force)
    stats = TriageStats()
    # Set once any call hits the budget cap, so later calls skip entirely
    # instead of each recording their own skipped_budget row.
    stop = threading.Event()

    cfg = llm.config.classifier
    p_by_id: dict[str, float] = {}
    if rows and cfg is not None and cfg.enabled and llm.has_classifier:
        rows, p_by_id = _classify_stage(conn, llm, rows, cfg, classify_concurrency, stats, stop)
        if stop.is_set():
            # Unfinished messages stay untriaged and are retried on the next run.
            stats.skipped_budget_batches += 1
            return stats

    batches = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
    jobs = [({r["id"] for r in b}, build_batch_input(conn, b)) for b in batches]
    system = load_prompt()

    def call(job: tuple[set[str], str]) -> tuple[set[str], LLMResponse | None, str | None]:
        ids, user = job
        if stop.is_set():
            return ids, None, "budget"
        try:
            resp = llm.complete(
                "triage", system, user, TRIAGE_SCHEMA, SCHEMA_NAME,
                validate=lambda data: parse_results(data, ids),
            )
            return ids, resp, None
        except BudgetExceeded:
            stop.set()
            return ids, None, "budget"
        except LLMError:
            return ids, None, "failed"

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(call, job) for job in jobs]
        for fut in as_completed(futures):
            ids, resp, error = fut.result()
            if error == "budget":
                stats.skipped_budget_batches += 1
                continue
            if error == "failed":
                stats.failed_batches += 1
                continue
            results = parse_results(resp.data, ids)
            now = to_iso(datetime.now(timezone.utc))
            with llm.db_lock, conn:
                conn.executemany(_INSERT, [
                    (r.message_id, r.sentiment, r.confidence, r.kind, json.dumps(list(r.topics)),
                     int(r.needs_reply), PROMPT_VERSION, resp.run_id, now,
                     p_by_id.get(r.message_id), None, "llm")
                    for r in results
                ])
            stats.triaged += len(results)
    return stats
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_triage_classifier.py tests/test_triage.py -v`
Expected: all pass (8 new; all existing `test_triage.py` tests unchanged and passing).

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/agents/triage.py tests/test_triage_classifier.py
git commit -m "feat: two-stage triage, Jev first pass with LLM escalation" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Mod queue priority listing and the `queue` command

**Files:**
- Modify: `pulse/modqueue.py`, `pulse/pipeline.py`, `pulse/run.py`
- Test: `tests/test_modqueue.py` (append), `tests/test_pipeline.py` (append), `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `jump_link` (links), `from_iso`, `to_iso` (models), triage `needs_reply_p` column (Task 2).
- Produces:
  - `list_open(conn, limit: int = 20) -> list[sqlite3.Row]` ordered frustrated first, then `COALESCE(needs_reply_p, needs_reply, 0)` descending, then oldest first. Row keys: `reason, message_id, guild_id, channel_id, channel_name, author_name, content, created_at, needs_reply_p`.
  - `format_queue(items, now: datetime) -> str` in `pulse/pipeline.py`.
  - CLI: `python -m pulse.run queue [--limit N]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_modqueue.py`:

```python
from pulse.modqueue import list_open


def set_p(conn, message_id, p):
    with conn:
        conn.execute("UPDATE triage SET needs_reply_p = ? WHERE message_id = ?", (p, message_id))


def test_list_open_orders_frustrated_then_probability_then_age():
    conn = db(msg("a", minutes=0), msg("b", minutes=10), msg("c", minutes=20), msg("d", minutes=30))
    for mid in ("a", "b", "c"):
        set_triage(conn, mid, needs_reply=True)
    set_triage(conn, "d", sentiment=-2)
    set_p(conn, "a", 0.72)
    set_p(conn, "b", 0.95)
    # c has no probability (LLM-only row with needs_reply=1), so it ranks as 1.0
    refresh_mod_queue(conn, CONFIG, NOW)
    assert [r["message_id"] for r in list_open(conn)] == ["d", "c", "b", "a"]
    assert len(list_open(conn, limit=2)) == 2


def test_list_open_excludes_closed_items():
    conn = db(msg("q1"))
    set_triage(conn, "q1", needs_reply=True)
    refresh_mod_queue(conn, CONFIG, NOW)
    with conn:
        conn.execute("UPDATE mod_queue SET status = 'dismissed'")
    assert list_open(conn) == []
```

Append to `tests/test_pipeline.py`:

```python
from datetime import timedelta

from pulse.modqueue import list_open, refresh_mod_queue
from pulse.pipeline import format_queue
from pulse.store import upsert_messages
from tests.fakes import T0, msg, set_triage


def test_format_queue_shows_author_channel_age_and_jump_link():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("q1", "how do I rotate keys?")], frozenset({"t1"}))
    set_triage(conn, "q1", needs_reply=True)
    now = T0 + timedelta(hours=24)
    refresh_mod_queue(conn, make_config(), now)
    text = format_queue(list_open(conn), now)
    assert "unanswered" in text
    assert "alice in #help" in text
    assert "24h ago" in text
    assert "how do I rotate keys?" in text
    assert "https://discord.com/channels/900/100/q1" in text


def test_format_queue_when_empty():
    assert format_queue([], T0) == "mod queue: nothing open"
```

Append to `tests/test_cli.py`:

```python
from datetime import datetime, timedelta, timezone

from pulse.models import Message
from pulse.store import upsert_messages


def test_queue_command_lists_open_items(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    conn = connect(tmp_path / "pulse.db")
    recent = datetime.now(timezone.utc) - timedelta(hours=1)
    upsert_messages(conn, [Message(id="f1", guild_id="900", channel_id="100", author_id="u1",
                                   author_name="erin", content="prod is down after the upgrade",
                                   created_at=recent, channel_name="help")], frozenset())
    with conn:
        conn.execute(
            "INSERT INTO triage (message_id, sentiment, confidence, kind, topics, needs_reply, prompt_version,"
            " created_at) VALUES ('f1', -2, 0.9, 'bug', '[]', 1, 'test', 'x')"
        )
    conn.close()

    assert main(["--config", str(tmp_path / "pulse.toml"), "modqueue"]) == 0
    assert main(["--config", str(tmp_path / "pulse.toml"), "queue", "--limit", "5"]) == 0

    out = capsys.readouterr().out
    assert "[frustrated" in out
    assert "erin in #help" in out
    assert "https://discord.com/channels/900/100/f1" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_modqueue.py tests/test_pipeline.py tests/test_cli.py -v`
Expected: FAIL with `ImportError: cannot import name 'list_open'`

- [ ] **Step 3: Implement**

Append to `pulse/modqueue.py`:

```python
def list_open(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Open items, highest priority first: frustrated, then Jev's needs_reply
    probability (an LLM-only row counts as its 0/1 label), then oldest."""
    return conn.execute(
        "SELECT q.reason, m.id AS message_id, m.guild_id, m.channel_id, m.channel_name, m.author_name,"
        " m.content, m.created_at, t.needs_reply_p"
        " FROM mod_queue q JOIN messages m ON m.id = q.message_id"
        " LEFT JOIN triage t ON t.message_id = m.id"
        " WHERE q.status = 'open'"
        " ORDER BY (q.reason = 'frustrated') DESC, COALESCE(t.needs_reply_p, t.needs_reply, 0) DESC,"
        " m.created_at ASC, m.id ASC"
        " LIMIT ?",
        (limit,),
    ).fetchall()
```

In `pulse/pipeline.py`, add imports:

```python
from pulse.links import jump_link
from pulse.models import from_iso
```

and append:

```python
def format_queue(items, now: datetime) -> str:
    if not items:
        return "mod queue: nothing open"
    lines = [f"mod queue: {len(items)} open, highest priority first"]
    for i, it in enumerate(items, start=1):
        age_h = (now - from_iso(it["created_at"])).total_seconds() / 3600
        p = it["needs_reply_p"]
        priority = f" p={p:.2f}" if p is not None else ""
        text = " ".join(it["content"].split())
        lines.append(
            f"{i:>2}. [{it['reason']}{priority}] {it['author_name']} in #{it['channel_name']},"
            f" {age_h:.0f}h ago: {text[:90]}"
        )
        lines.append(f"    {jump_link(it['guild_id'], it['channel_id'], it['message_id'])}")
    return "\n".join(lines)
```

In `pulse/run.py`:
- import `list_open` from `pulse.modqueue` and `format_queue` from `pulse.pipeline`;
- in `_parser`, after the `modqueue` subparser:

```python
    queue = sub.add_parser("queue", help="list open mod queue items, highest priority first")
    queue.add_argument("--limit", type=int, default=20)
```

- in `main`, add a branch:

```python
    elif args.command == "queue":
        print(format_queue(list_open(conn, args.limit), now))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_modqueue.py tests/test_pipeline.py tests/test_cli.py -v` — Expected: all pass.

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/modqueue.py pulse/pipeline.py pulse/run.py tests/test_modqueue.py tests/test_pipeline.py tests/test_cli.py
git commit -m "feat: priority-ordered mod queue listing and queue command with jump links" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: Wire Jev into the pipeline, report it, document it

**Files:**
- Modify: `pulse/pipeline.py`, `pulse.toml.example`, `README.md`
- Test: `tests/test_pipeline.py` (append)

**Interfaces:**
- Consumes: `JevBackend` (Task 3), `LLMClient(classifier=)` (Task 4), `TriageStats` Jev fields (Task 5).
- Produces: `build_llm` attaches a `JevBackend` when `config.classifier` is enabled; `format_triage` adds a `jev:` line when the classifier did anything.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_pipeline.py`:

```python
from pulse.agents.triage import TriageStats
from pulse.pipeline import build_llm, format_triage
from tests.fakes import classifier_config


def test_build_llm_attaches_jev_only_when_enabled(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("OPENROUTER_API_KEY", "y")
    assert build_llm(connect(":memory:"), make_config(classifier=classifier_config())).has_classifier is True
    assert build_llm(connect(":memory:"), make_config(classifier=classifier_config(enabled=False))).has_classifier is False
    assert build_llm(connect(":memory:"), make_config()).has_classifier is False


def test_format_triage_reports_jev_split():
    text = format_triage(TriageStats(triaged=10, jev_labeled=7, escalated=3, classifier_failed=1))
    assert "jev: labeled 7, escalated to LLM 3, classifier failures 1" in text


def test_format_triage_without_jev_has_no_jev_line():
    assert "jev" not in format_triage(TriageStats(triaged=4))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_pipeline.py -v`
Expected: FAIL (`has_classifier` is False when enabled; no `jev:` line)

- [ ] **Step 3: Implement**

In `pulse/pipeline.py`, replace `build_llm`:

```python
def build_llm(conn: sqlite3.Connection, config: Config) -> LLMClient:
    classifier = None
    if config.classifier is not None and config.classifier.enabled:
        from pulse.agents.providers.jev_backend import JevBackend

        classifier = JevBackend()
    return LLMClient(conn, config, build_backends(config), classifier=classifier)
```

Replace `format_triage`:

```python
def format_triage(stats: TriageStats) -> str:
    line = f"triage: triaged {stats.triaged}, failed batches {stats.failed_batches}"
    if stats.jev_labeled or stats.escalated or stats.classifier_failed:
        line += (
            f"\n  jev: labeled {stats.jev_labeled}, escalated to LLM {stats.escalated},"
            f" classifier failures {stats.classifier_failed}"
        )
    if stats.skipped_budget_batches:
        line += f"\n  daily budget cap reached: {stats.skipped_budget_batches} batches skipped"
    return line
```

Append to `pulse.toml.example` (before the commented `[[launches]]` block):

```toml
# Jev first pass (optional): labels every message cheaply and sends only the
# ones that matter to the triage LLM. Uses OPENROUTER_API_KEY.
# See docs/benchmarks/2026-09-30-triage-llm-vs-jev.md before enabling.
[classifier]
enabled = false
model = "jev:jev-latest"
needs_reply_threshold = 0.7
min_confidence = 0.6
escalate_kinds = ["bug", "docs", "feature_request", "praise"]

# Jev reports its own cost per request; this is only a fallback.
# [pricing."jev:jev-latest"]
# per_request = 0.0000387
```

Add to `README.md`, after the Commands section:

````markdown
## Jev first pass (optional)

With `[classifier] enabled = true`, triage asks Jev (a cheap closed-set classifier, via OpenRouter) about every message first. Confident, low-stakes messages are labelled by Jev alone; anything negative, likely to need a reply, a bug, docs issue, feature request or praise, or low-confidence, still goes to the triage LLM for full labels and topics. Staff messages are always neutral. If Jev fails on a message, the LLM handles it.

Measured on synthetic data this cut triage cost by roughly 2-3x at the same needs-reply accuracy (see `docs/benchmarks/`). Re-check on your own data before relying on it.

List the mod queue, highest priority first, with links to each message:

```bash
.venv/bin/python -m pulse.run queue --limit 20
```
````

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_pipeline.py -v` — Expected: all pass.

- [ ] **Step 5: Full suite, then commit**

Run: `.venv/bin/pytest -q` — Expected: all pass.

```bash
git add pulse/pipeline.py pulse.toml.example README.md tests/test_pipeline.py
git commit -m "feat: wire Jev classifier into the pipeline, report and document it" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
