# Discord Pulse Plan 5: Accuracy Check, Bot, Schedules and Integrations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish v1: an accuracy check that scores triage models (and Jev) against a hand-labelled set with the 90% Jev gate, a read-only Discord bot (one-off backfill and a live streamer) with a pitch doc for the mod team, launchd schedules, sending a pain point to GitHub or Linear, Slack alerts, and three dashboard follow-ups.

**Architecture:** `pulse/eval.py` loads a JSONL gold set into an in-memory database and reuses the real triage code (`run_triage`, `build_batch_input`, `LLMClient.classify`), so it measures exactly what production does without touching `pulse.db`. The bot splits into a discord-free layer (`pulse/sources/bot_source.py`: message mapping, async backfill over duck-typed channels, the `BotStreamer` buffer) that is fully tested with fakes, and a thin discord.py layer (`pulse/sources/bot_client.py`) imported only when the bot runs. Outbound integrations (`pulse/issues.py`, `pulse/alerts.py`) take an injectable `httpx.Client`, store what they sent so nothing is posted twice, and read tokens from the environment at the moment of use.

**Tech Stack:** Python 3.12, sqlite3, httpx (already a dependency), discord.py 2.x as an optional extra (`pip install -e '.[bot]'`), plistlib + launchctl, FastAPI/Jinja for the one dashboard addition, pytest.

**Spec:** `docs/superpowers/specs/2026-09-29-discord-pulse-design.md` (sections 2, 3, 9, 10, 11, 13, 14.5, 15.6).

## Global Constraints

- Secrets come only from environment variables, never `pulse.toml`, never a plist, never a log line: `DISCORD_BOT_TOKEN`, `GITHUB_TOKEN`, `LINEAR_API_KEY`, `SLACK_WEBHOOK_URL`, plus the existing provider keys.
- The bot is read-only. It never sends, reacts, edits, deletes, DMs or changes anything. Gateway intents: guilds, guild messages and message content only. Invite permissions: View Channels (1024) + Read Message History (65536) = `66560`, scope `bot`.
- `discord` is imported only inside functions in `pulse/sources/bot_client.py`. No test imports discord.py; it is an optional extra.
- Tests never touch the network: HTTP goes through an injected `httpx.Client(transport=httpx.MockTransport(...))`, launchctl through an injected runner.
- The accuracy check never writes to the configured database; every run uses its own in-memory database and its own spending cap (`--max-usd`, default 1.00 per model).
- Issues sent to GitHub or Linear contain the pain point summary, counts and up to 8 message excerpts with Discord links, and no author names. Anything posted outside the machine happens only when the user clicks or runs a command, or has turned alerts on.
- Nothing is posted twice: issues are keyed on (root theme, tracker); alerts on (kind, key).
- Jev gate (spec 14.5): a `jev:` row whose needs-reply agreement with the gold set is below 90% prints a recommendation to set `[classifier] enabled = false`. Nothing switches off automatically.
- Timestamps are stored with `pulse.models.to_iso`. Every commit message ends with exactly `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A gold file with broken lines (bad JSON, unknown kind, sentiment `true`, missing labels, duplicate ids) or still-unlabelled rows: the check reports each bad line by number, counts unlabelled rows, scores the rest and never crashes (Task 1 tests).
2. A channel or thread the bot cannot read, or one deleted mid-backfill: the error names the channel and the other channels still backfill (Task 3 test `test_backfill_continues_past_unreadable_channel`).
3. The live bot flushing while `pipeline` holds the write lock: the batch stays buffered and is written on the next flush; no message is lost (Task 4 test `test_flush_keeps_messages_when_database_is_locked`).
4. Sending the same pain point twice, or sending a merged theme: one issue per root theme and tracker; the second request returns the existing link (Task 6 test `test_send_issue_is_idempotent_and_uses_root_theme`).
5. Alerts with `pipeline` every 30 minutes: each spike alerts once per theme per UTC day, each frustrated item once, and a failed webhook post is retried next run instead of being marked sent (Task 8 tests).

## Files

| File | Responsibility | Task |
|---|---|---|
| `eval/gold.example.jsonl` | 141 labelled synthetic messages (already committed with this plan) | — |
| `pulse/eval.py` | gold loading, metrics, eval runs, gate, CLI, `--sample` | 1, 2 |
| `pulse/config.py` | `require_keys`, `missing_keys`, `bot_backfill_days`, `[integrations]`, `[alerts]` | 2, 3, 6, 8 |
| `pulse/sources/bot_source.py` | discord-free mapping, backfill, `BotSource`, `BotStreamer` | 3, 4 |
| `pulse/sources/bot_client.py` | discord.py glue: client, `run_backfill`, `run_streamer` | 3, 4 |
| `pulse/schedule.py` | launchd plists, install/uninstall | 5 |
| `pulse/issues.py` | issue drafts, GitHub/Linear clients, `send_issue` | 6 |
| `pulse/alerts.py` | spike and frustrated-user alerts, Slack webhook | 8 |
| `pulse/web/views/pain.py`, `pain.html` | "Send to GitHub/Linear" on Pain points | 7 |
| `pulse/run.py` | `ingest --source`, `bot`, `bot-invite`, `schedule`, `issue`, `alerts`, `web` without keys | 3–9 |
| `docs/bot-pitch.md` | the one-pager for the mod team | 4 |

---

### Task 1: Gold set loading and metrics

**Files:**
- Create: `pulse/eval.py`
- Test: `tests/test_eval_gold.py`

**Interfaces:**
- Consumes: `pulse.models.KINDS`, `Message`, `parse_timestamp`.
- Produces: `GoldRow(message: Message, is_team: bool, sentiment: int, kind: str, needs_reply: bool)`; `GoldSet(rows: list[GoldRow], errors: list[str], unlabeled: int)`; `load_gold(path) -> GoldSet` (raises `FileNotFoundError` for a missing file); `Prediction(sentiment: int, kind: str, needs_reply: bool)`; `Scores(n, missing, sentiment_exact, sentiment_within_1, kind_macro_f1, needs_reply_recall: float | None, needs_reply_precision: float | None, needs_reply_agreement)`; `score(gold: list[GoldRow], preds: dict[str, Prediction]) -> Scores` (raises `ValueError` on an empty list); `macro_f1(pairs) -> float`.

Gold line format (one JSON object per line):

```json
{"message_id": "1", "guild_id": "900", "channel_id": "100", "channel_name": "help", "thread_id": null,
 "parent_channel_id": null, "author_id": "u1", "author_name": "alice", "is_team": false,
 "created_at": "2026-09-28T12:00:00Z", "content": "install fails on M1", "reply_to_id": null,
 "labels": {"sentiment": -1, "kind": "bug", "needs_reply": true}}
```

Required: `message_id`, `channel_id`, `author_id`, `author_name`, `created_at`, `content` (non-empty), and `labels` with all three keys. A row whose labels contain any `null` is "not yet labelled": counted, not an error. A missing prediction counts as wrong for every metric.

- [ ] **Step 1: Write the failing tests**

`tests/test_eval_gold.py`:

```python
import json

import pytest

from pulse.eval import GoldRow, Prediction, load_gold, macro_f1, score


def line(mid, *, labels=..., **over):
    raw = {
        "message_id": mid, "channel_id": "100", "channel_name": "help", "author_id": "u1",
        "author_name": "alice", "created_at": "2026-09-28T12:00:00Z", "content": f"message {mid}",
        "labels": {"sentiment": -1, "kind": "bug", "needs_reply": True} if labels is ... else labels,
    }
    raw.update(over)
    return json.dumps(raw)


def write(tmp_path, lines):
    path = tmp_path / "gold.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_load_gold_parses_rows_and_reports_bad_lines(tmp_path):
    path = write(tmp_path, [
        line("1"),
        line("2", labels={"sentiment": None, "kind": None, "needs_reply": None}),
        "{not json",
        line("4", labels={"sentiment": 0, "kind": "complaint", "needs_reply": False}),
        line("5", labels={"sentiment": True, "kind": "bug", "needs_reply": True}),
        "",
        line("1"),
        line("8", content=""),
        line("9", labels={"sentiment": 0, "kind": "bug"}),
        line("10", is_team=True, thread_id="300", parent_channel_id="100", reply_to_id="1"),
    ])
    gold = load_gold(path)
    assert [r.message.id for r in gold.rows] == ["1", "10"]
    assert gold.unlabeled == 1
    assert [e.split(":")[0] for e in gold.errors] == ["line 3", "line 4", "line 5", "line 7", "line 8", "line 9"]
    assert "unknown kind 'complaint'" in gold.errors[1]
    assert "duplicate message_id 1" in gold.errors[3]
    ten = gold.rows[1]
    assert ten.is_team and ten.message.thread_id == "300" and ten.message.parent_channel_id == "100"
    assert ten.message.reply_to_id == "1" and ten.message.guild_id == "0" and ten.message.source == "eval"
    assert ten.message.created_at.tzinfo is not None


def test_load_gold_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_gold(tmp_path / "nope.jsonl")


def test_shipped_example_gold_set_loads_cleanly():
    gold = load_gold("eval/gold.example.jsonl")
    assert gold.errors == [] and gold.unlabeled == 0
    assert len(gold.rows) == 141
    assert sum(r.needs_reply for r in gold.rows) == 67


def _gold(mid, sentiment, kind, needs_reply):
    from pulse.models import Message
    from tests.fakes import T0

    m = Message(id=mid, guild_id="0", channel_id="100", author_id="u1", author_name="a", content="x", created_at=T0)
    return GoldRow(m, False, sentiment, kind, needs_reply)


def test_score_counts_missing_predictions_as_wrong():
    gold = [_gold("1", -1, "bug", True), _gold("2", 0, "question", True),
            _gold("3", 1, "praise", False), _gold("4", 0, "other", False)]
    preds = {"1": Prediction(-2, "bug", True), "2": Prediction(0, "docs", False), "3": Prediction(1, "praise", True)}
    s = score(gold, preds)
    assert s.n == 4 and s.missing == 1
    assert s.sentiment_exact == 0.5 and s.sentiment_within_1 == 0.75
    assert s.needs_reply_agreement == 0.25
    assert s.needs_reply_recall == 0.5 and s.needs_reply_precision == 0.5
    assert s.kind_macro_f1 == pytest.approx(0.5)


def test_score_perfect_and_empty_cases():
    gold = [_gold("1", 1, "praise", False)]
    s = score(gold, {"1": Prediction(1, "praise", False)})
    assert s.sentiment_exact == 1.0 and s.kind_macro_f1 == 1.0 and s.needs_reply_agreement == 1.0
    assert s.needs_reply_recall is None and s.needs_reply_precision is None
    with pytest.raises(ValueError):
        score([], {})


def test_macro_f1_averages_gold_kinds_only():
    assert macro_f1([("bug", "bug"), ("bug", "docs")]) == pytest.approx(2 / 3)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_eval_gold.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.eval'`

- [ ] **Step 3: Write the implementation**

`pulse/eval.py`:

```python
"""Accuracy check for triage: score models against a hand-labelled gold set.

    python -m pulse.eval [--model provider:model ...] [--gold eval/gold.jsonl]
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from pulse.models import KINDS, Message, parse_timestamp

REQUIRED = ("message_id", "channel_id", "author_id", "author_name", "created_at", "content")
LABELS = ("sentiment", "kind", "needs_reply")


@dataclass(frozen=True)
class GoldRow:
    message: Message
    is_team: bool
    sentiment: int
    kind: str
    needs_reply: bool


@dataclass
class GoldSet:
    rows: list[GoldRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unlabeled: int = 0


def _opt(raw: dict, key: str) -> str | None:
    value = raw.get(key)
    return None if value in (None, "") else str(value)


def _row(raw) -> GoldRow | None:
    """One parsed line, or None when any label is still null (not yet labelled)."""
    if not isinstance(raw, dict):
        raise ValueError("not a JSON object")
    missing = [k for k in REQUIRED if raw.get(k) in (None, "")]
    if missing:
        raise ValueError(f"missing {', '.join(missing)}")
    labels = raw.get("labels")
    if not isinstance(labels, dict):
        raise ValueError("labels must be an object with sentiment, kind and needs_reply")
    absent = [k for k in LABELS if k not in labels]
    if absent:
        raise ValueError(f"labels missing {', '.join(absent)}")
    if any(labels[k] is None for k in LABELS):
        return None
    sentiment, kind, needs_reply = labels["sentiment"], labels["kind"], labels["needs_reply"]
    if isinstance(sentiment, bool) or not isinstance(sentiment, int) or not -2 <= sentiment <= 2:
        raise ValueError("sentiment must be an integer from -2 to 2")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; allowed {list(KINDS)}")
    if not isinstance(needs_reply, bool):
        raise ValueError("needs_reply must be true or false")
    message = Message(
        id=str(raw["message_id"]),
        guild_id=str(raw.get("guild_id") or "0"),
        channel_id=str(raw["channel_id"]),
        channel_name=str(raw.get("channel_name") or ""),
        author_id=str(raw["author_id"]),
        author_name=str(raw["author_name"]),
        content=str(raw["content"]),
        created_at=parse_timestamp(str(raw["created_at"])),
        thread_id=_opt(raw, "thread_id"),
        parent_channel_id=_opt(raw, "parent_channel_id"),
        reply_to_id=_opt(raw, "reply_to_id"),
        source="eval",
    )
    return GoldRow(message, bool(raw.get("is_team", False)), sentiment, kind, needs_reply)


def load_gold(path: str | Path) -> GoldSet:
    gold = GoldSet()
    seen: set[str] = set()
    with Path(path).open(encoding="utf-8") as fh:
        for n, text in enumerate(fh, start=1):
            if not text.strip():
                continue
            try:
                row = _row(json.loads(text))
            except (ValueError, TypeError) as e:
                gold.errors.append(f"line {n}: {e}")
                continue
            if row is None:
                gold.unlabeled += 1
                continue
            if row.message.id in seen:
                gold.errors.append(f"line {n}: duplicate message_id {row.message.id}")
                continue
            seen.add(row.message.id)
            gold.rows.append(row)
    return gold


@dataclass(frozen=True)
class Prediction:
    sentiment: int
    kind: str
    needs_reply: bool


@dataclass(frozen=True)
class Scores:
    n: int
    missing: int
    sentiment_exact: float
    sentiment_within_1: float
    kind_macro_f1: float
    needs_reply_recall: float | None
    needs_reply_precision: float | None
    needs_reply_agreement: float


def _ratio(a: int, b: int) -> float | None:
    return a / b if b else None


def macro_f1(pairs: list[tuple[str, str | None]]) -> float:
    """Mean F1 over the kinds present in the gold labels. A missing prediction
    (None) counts as a miss for its gold kind."""
    f1s = []
    for kind in sorted({g for g, _ in pairs}):
        tp = sum(1 for g, p in pairs if g == kind and p == kind)
        fp = sum(1 for g, p in pairs if g != kind and p == kind)
        fn = sum(1 for g, p in pairs if g == kind and p != kind)
        f1s.append(0.0 if tp == 0 else 2 * tp / (2 * tp + fp + fn))
    return sum(f1s) / len(f1s)


def score(gold: list[GoldRow], preds: dict[str, Prediction]) -> Scores:
    n = len(gold)
    if n == 0:
        raise ValueError("no labelled rows to score")
    exact = within = agree = tp = fp = fn = missing = 0
    kind_pairs: list[tuple[str, str | None]] = []
    for row in gold:
        p = preds.get(row.message.id)
        if p is None:
            missing += 1
            fn += row.needs_reply
            kind_pairs.append((row.kind, None))
            continue
        exact += p.sentiment == row.sentiment
        within += abs(p.sentiment - row.sentiment) <= 1
        agree += p.needs_reply == row.needs_reply
        if p.needs_reply and row.needs_reply:
            tp += 1
        elif p.needs_reply:
            fp += 1
        elif row.needs_reply:
            fn += 1
        kind_pairs.append((row.kind, p.kind))
    return Scores(
        n=n,
        missing=missing,
        sentiment_exact=exact / n,
        sentiment_within_1=within / n,
        kind_macro_f1=macro_f1(kind_pairs),
        needs_reply_recall=_ratio(tp, tp + fn),
        needs_reply_precision=_ratio(tp, tp + fp),
        needs_reply_agreement=agree / n,
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_eval_gold.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add pulse/eval.py tests/test_eval_gold.py
git commit -m "feat: gold set loading and triage accuracy metrics

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Eval runs, the Jev gate and `python -m pulse.eval`

**Files:**
- Modify: `pulse/config.py` (`load_config(..., require_keys=True)`, `_classifier(raw, env, require_keys)`, new `missing_keys`)
- Modify: `pulse/eval.py` (append)
- Modify: `README.md` (new "Accuracy check" section)
- Test: `tests/test_eval_run.py`, `tests/test_config.py` (append)

**Interfaces:**
- Consumes: Task 1; `pulse.agents.llm.LLMClient(conn, config, backends, *, now=None, classifier=None)` with `.classify(state) -> ClassifyResponse(result, run_id)`; `pulse.agents.triage.run_triage(conn, llm)`, `select_untriaged(conn)`, `build_batch_input(conn, rows) -> str` (JSON `{"messages": [state, ...]}`); `pulse.pipeline.build_backends(config)`; `pulse.store.upsert_messages`; `pulse.db.connect`.
- Produces: `load_config(path, env=None, *, require_keys=True)`; `missing_keys(config, env=None) -> list[str]` (sorted env-var names needed by `config.models` and an enabled classifier but unset); in `pulse/eval.py`: `GATE = 0.90`, `HYBRID = "hybrid"`, `DEFAULT_MAX_USD = 1.00`, `EvalResult(spec, scores, cost_usd, notes)`, `default_specs(config) -> list[str]`, `keys_for_specs(specs, config, env) -> list[str]` (missing ones), `run_eval(gold, config, specs, *, backends_for, classifier_for, max_usd, now=None) -> list[EvalResult]` (raises `ConfigError` for an unknown spec, a non-OpenRouter model without pricing, or `hybrid` with no `[classifier]`), `gate_lines(results) -> list[str]`, `format_results(gold, results) -> str`, `write_sample(conn, out, n, *, seed=0) -> int` (raises `FileExistsError`), `main(argv=None, *, env=None, backends_for=None, classifier_for=None) -> int`.

Spec strings: `provider:model` (LLM-only triage with that model), `jev:<model>` (Jev alone, staff messages overridden to neutral exactly as production does, needs-reply = probability ≥ the configured threshold, default 0.7), `hybrid` (production two-stage triage: Jev first, the `[models] triage` model for escalations). Default specs: the configured triage model, plus `jev:<model>` and `hybrid` when `[classifier] enabled = true`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_require_keys_false_skips_env_checks(tmp_path):
    from pulse.config import load_config, missing_keys

    path = tmp_path / "pulse.toml"
    path.write_text(
        '[server]\nguild_id = "1"\n[models]\ntriage = "openai:t"\ntheme = "openai:t"\n'
        'digest = "anthropic:d"\ninvestigate = "openai:t"\n'
        '[pricing."openai:t"]\ninput = 1\noutput = 2\n[pricing."anthropic:d"]\ninput = 1\noutput = 2\n'
        '[classifier]\nenabled = true\n'
    )
    config = load_config(path, {}, require_keys=False)
    assert config.classifier.enabled
    assert missing_keys(config, {"OPENAI_API_KEY": "x"}) == ["ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"]
    assert missing_keys(config, {"OPENAI_API_KEY": "x", "ANTHROPIC_API_KEY": "y", "OPENROUTER_API_KEY": "z"}) == []
```

`tests/test_eval_run.py`:

```python
import json

import pytest

from pulse.agents.base import BackendResult
from pulse.config import ConfigError, ModelRef
from pulse.eval import GATE, gate_lines, load_gold, main, run_eval, write_sample
from pulse.db import connect
from pulse.store import upsert_messages
from tests.fakes import FIXED_NOW, FakeBackend, FakeClassifier, classifier_config, jev_result, make_config, msg

ROWS = [
    ("g1", "it is broken", -1, "bug", True, "u1", False),
    ("g2", "love it", 1, "praise", False, "u2", False),
    ("g3", "broken again?", -2, "bug", True, "u3", False),
    ("g4", "fixed in 2.1", 0, "other", False, "t1", True),
]


def gold_path(tmp_path):
    path = tmp_path / "gold.jsonl"
    lines = []
    for i, (mid, content, s, k, nr, author, team) in enumerate(ROWS):
        lines.append(json.dumps({
            "message_id": mid, "channel_id": "100", "channel_name": "help", "author_id": author,
            "author_name": author, "is_team": team, "created_at": f"2026-09-28T12:0{i}:00Z",
            "content": content, "labels": {"sentiment": s, "kind": k, "needs_reply": nr},
        }))
    path.write_text("\n".join(lines) + "\n")
    return path


def llm_handler(user):
    items = json.loads(user)["messages"]
    return BackendResult({"results": [
        {"message_id": m["message_id"], "sentiment": -1 if "broken" in m["content"] else 1, "confidence": 0.9,
         "kind": "bug" if "broken" in m["content"] else "praise", "topics": [],
         "needs_reply": "broken" in m["content"]}
        for m in items
    ]}, 100, 20)


def jev_handler(state):
    if "broken" in state["content"]:
        return jev_result(p=0.9, kind="bug", sentiment=-1)
    return jev_result(p=0.1, kind="praise", sentiment=1)


def run(tmp_path, specs, *, backend=None, classifier=None, max_usd=1.0, **config_over):
    gold = load_gold(gold_path(tmp_path))
    config = make_config(db_path=tmp_path / "pulse.db", **config_over)
    backend = backend or FakeBackend(handler=llm_handler)
    classifier = classifier or FakeClassifier(handler=jev_handler)
    results = run_eval(
        gold, config, specs, backends_for=lambda cfg: {"anthropic": backend},
        classifier_for=lambda: classifier, max_usd=max_usd, now=lambda: FIXED_NOW,
    )
    return gold, results, backend, classifier


def test_llm_eval_scores_and_costs_without_touching_the_real_db(tmp_path):
    _, [r], backend, _ = run(tmp_path, ["anthropic:m-triage"])
    s = r.scores
    assert s.sentiment_exact == 0.5 and s.sentiment_within_1 == 1.0
    assert s.needs_reply_agreement == 1.0 and s.needs_reply_recall == 1.0 and s.missing == 0
    assert s.kind_macro_f1 == pytest.approx((1 + 0 + 2 / 3) / 3)
    assert r.cost_usd == pytest.approx(0.0002)
    assert [c["model"] for c in backend.calls] == ["m-triage"]
    assert not (tmp_path / "pulse.db").exists()


def test_jev_eval_overrides_staff_and_passes_the_gate(tmp_path):
    _, [r], backend, classifier = run(tmp_path, ["jev:jev-latest"])
    assert backend.calls == [] and len(classifier.calls) == 4
    assert r.scores.sentiment_exact == 0.75 and r.scores.needs_reply_agreement == 1.0
    assert r.cost_usd == pytest.approx(0.00008)
    assert gate_lines([r]) == ["jev:jev-latest: needs-reply agreement 100% meets the 90% gate."]


def test_jev_below_gate_recommends_turning_it_off(tmp_path):
    bad = FakeClassifier(handler=lambda s: jev_result(p=0.1, kind="praise", sentiment=1))
    _, [r], _, _ = run(tmp_path, ["jev:jev-latest"], classifier=bad)
    assert r.scores.needs_reply_agreement == 0.5 < GATE
    assert gate_lines([r]) == [
        "jev:jev-latest: needs-reply agreement 50% is below the 90% gate. "
        "Recommend setting [classifier] enabled = false."
    ]


def test_jev_threshold_comes_from_config(tmp_path):
    gold = load_gold(gold_path(tmp_path))
    config = make_config(classifier=classifier_config(needs_reply_threshold=0.95))
    [r] = run_eval(gold, config, ["jev:jev-latest"], backends_for=lambda c: {},
                   classifier_for=lambda: FakeClassifier(handler=jev_handler), max_usd=1.0, now=lambda: FIXED_NOW)
    assert r.scores.needs_reply_recall == 0.0  # p = 0.9 is below the 0.95 threshold


def test_hybrid_runs_two_stage_triage(tmp_path):
    gold = load_gold(gold_path(tmp_path))
    backend, classifier = FakeBackend(handler=llm_handler), FakeClassifier(handler=jev_handler)
    config = make_config(classifier=classifier_config())
    [r] = run_eval(gold, config, ["hybrid"], backends_for=lambda c: {"anthropic": backend},
                   classifier_for=lambda: classifier, max_usd=1.0, now=lambda: FIXED_NOW)
    assert len(classifier.calls) == 4 and len(backend.calls) == 1  # the three non-staff messages escalate in one batch (negative or praise)
    assert r.scores.missing == 0


def test_bad_specs_raise_config_error(tmp_path):
    gold = load_gold(gold_path(tmp_path))
    for spec, config in [
        ("hybrid", make_config()),
        ("nope:model", make_config()),
        ("openai:gpt-x", make_config()),
    ]:
        with pytest.raises(ConfigError):
            run_eval(gold, config, [spec], backends_for=lambda c: {}, classifier_for=FakeClassifier, max_usd=1.0)


def test_budget_cap_leaves_messages_missing(tmp_path):
    _, [r], _, _ = run(tmp_path, ["anthropic:m-triage"], max_usd=0.0)
    assert r.scores.missing == 4 and r.cost_usd == 0.0
    assert any("skipped_budget" in n for n in r.notes)


TOML = """
[server]
guild_id = "900"
team_member_ids = ["t1"]
[models]
triage = "anthropic:m-triage"
theme = "anthropic:m-theme"
digest = "anthropic:m-digest"
investigate = "anthropic:m-investigate"
[pricing."anthropic:m-triage"]
input = 1.0
output = 5.0
[pricing."anthropic:m-theme"]
input = 1.0
output = 5.0
[pricing."anthropic:m-digest"]
input = 1.0
output = 5.0
[pricing."anthropic:m-investigate"]
input = 1.0
output = 5.0
"""


def cli(tmp_path, argv, env=None, capsys=None):
    (tmp_path / "pulse.toml").write_text(TOML)
    backend = FakeBackend(handler=llm_handler)
    return main(
        ["--config", str(tmp_path / "pulse.toml"), *argv],
        env={"ANTHROPIC_API_KEY": "x"} if env is None else env,
        backends_for=lambda cfg: {"anthropic": backend},
        classifier_for=lambda: FakeClassifier(handler=jev_handler),
    )


def test_cli_prints_a_table_and_writes_json(tmp_path, capsys):
    out_json = tmp_path / "r.json"
    code = cli(tmp_path, ["--gold", str(gold_path(tmp_path)), "--json", str(out_json)])
    out = capsys.readouterr().out
    assert code == 0
    assert "4 labelled messages" in out and "reply recall" in out and "anthropic:m-triage" in out
    data = json.loads(out_json.read_text())
    assert data[0]["model"] == "anthropic:m-triage" and data[0]["needs_reply_agreement"] == 1.0


def test_cli_missing_key_and_missing_gold_exit_2(tmp_path, capsys):
    assert cli(tmp_path, ["--gold", str(gold_path(tmp_path))], env={}) == 2
    assert "ANTHROPIC_API_KEY must be set" in capsys.readouterr().err
    assert cli(tmp_path, ["--gold", str(tmp_path / "none.jsonl")]) == 2
    assert "--sample" in capsys.readouterr().err


def test_sample_writes_unlabelled_rows_and_refuses_to_overwrite(tmp_path, capsys):
    conn = connect(tmp_path / "pulse.db")
    upsert_messages(conn, [msg(f"m{i}", f"text {i}", minutes=i) for i in range(5)] + [msg("b", "bot", is_bot=True)],
                    frozenset({"t1"}))
    out = tmp_path / "label-me.jsonl"
    assert write_sample(conn, out, 3) == 3
    rows = [json.loads(x) for x in out.read_text().splitlines()]
    assert all(r["labels"] == {"sentiment": None, "kind": None, "needs_reply": None} for r in rows)
    assert all(r["message_id"] != "b" for r in rows)
    assert load_gold(out).unlabeled == 3
    with pytest.raises(FileExistsError):
        write_sample(conn, out, 3)
    conn.close()
    assert cli(tmp_path, ["--sample", "2", "--out", str(tmp_path / "new.jsonl")]) == 0
    assert "fill in each labels object" in capsys.readouterr().out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_eval_run.py tests/test_config.py -q`
Expected: FAIL (`ImportError` for `run_eval`; `TypeError` for `require_keys`)

- [ ] **Step 3: Write the implementation**

In `pulse/config.py`, change `_classifier` and `load_config` and add `missing_keys`:

```python
def _classifier(raw: Any, env: Mapping[str, str], require_keys: bool = True) -> ClassifierConfig | None:
    # ...body unchanged down to the key check, which becomes:
    if require_keys and cfg.enabled and not env.get(KEY_ENV[model.provider]):
        raise ConfigError(f"{KEY_ENV[model.provider]} must be set because the classifier {model} is enabled")
    return cfg


def load_config(path: str | Path, env: Mapping[str, str] | None = None, *, require_keys: bool = True) -> Config:
    # ...unchanged, except the provider loop's key check:
    for ref in models.values():
        var = KEY_ENV[ref.provider]
        if require_keys and not env.get(var):
            raise ConfigError(f"{var} must be set because {ref} is configured")
        if ref.provider != "openrouter" and str(ref) not in pricing:
            raise ConfigError(f'[pricing."{ref}"] is required so the budget cap can be enforced')
    # ...and the return passes classifier=_classifier(raw.get("classifier"), env, require_keys)


def missing_keys(config: Config, env: Mapping[str, str] | None = None) -> list[str]:
    """Env vars the configured models (and an enabled classifier) need but that are unset."""
    env = os.environ if env is None else env
    needed = {KEY_ENV[r.provider] for r in config.models.values()}
    if config.classifier is not None and config.classifier.enabled:
        needed.add(KEY_ENV[config.classifier.model.provider])
    return sorted(v for v in needed if not env.get(v))
```

Append to `pulse/eval.py` (move the new imports to the top of the file):

```python
import argparse
import os
import random
import sqlite3
import sys
from dataclasses import asdict, replace
from datetime import datetime
from typing import Callable, Mapping

from pulse.agents.base import Backend, BudgetExceeded, LLMError
from pulse.agents.classifier import Classifier
from pulse.agents.llm import LLMClient
from pulse.agents.triage import build_batch_input, run_triage, select_untriaged
from pulse.config import (
    CLASSIFIER_PROVIDERS, KEY_ENV, PROVIDERS, ClassifierConfig, Config, ConfigError, ModelRef, load_config,
)
from pulse.db import connect
from pulse.pipeline import build_backends
from pulse.store import upsert_messages

GATE = 0.90
HYBRID = "hybrid"
DEFAULT_MAX_USD = 1.00
_MEMORY = Path(":memory:")


@dataclass(frozen=True)
class EvalResult:
    spec: str
    scores: Scores
    cost_usd: float
    notes: tuple[str, ...] = ()


def _load(gold: list[GoldRow]) -> sqlite3.Connection:
    """A throwaway in-memory database holding just the gold messages."""
    conn = connect(":memory:")
    team = frozenset(r.message.author_id for r in gold if r.is_team)
    upsert_messages(conn, [r.message for r in gold], team)
    return conn


def _cost(conn: sqlite3.Connection) -> float:
    return float(conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM agent_runs").fetchone()[0])


def _notes(conn: sqlite3.Connection) -> tuple[str, ...]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM agent_runs WHERE status != 'ok' GROUP BY status ORDER BY status"
    ).fetchall()
    return tuple(f"{r['n']} {r['status']} call(s)" for r in rows)


def _triage_only(config: Config) -> Config:
    # Backends are built only for the triage model's provider, so keys for other agents are not needed.
    return replace(config, models={"triage": config.models["triage"]})


def _eval_triage(gold, config, backends, *, classifier=None, classifier_cfg=None, max_usd, now):
    conn = _load(gold)
    cfg = replace(config, classifier=classifier_cfg, daily_usd_cap=max_usd, db_path=_MEMORY)
    run_triage(conn, LLMClient(conn, cfg, backends, now=now, classifier=classifier))
    preds = {
        r["message_id"]: Prediction(r["sentiment"], r["kind"], bool(r["needs_reply"]))
        for r in conn.execute("SELECT message_id, sentiment, kind, needs_reply FROM triage")
    }
    return preds, _cost(conn), _notes(conn)


def _eval_jev(gold, config, classifier, cfg: ClassifierConfig, *, max_usd, now):
    conn = _load(gold)
    run_cfg = replace(config, classifier=cfg, daily_usd_cap=max_usd, db_path=_MEMORY)
    llm = LLMClient(conn, run_cfg, {}, now=now, classifier=classifier)
    rows = select_untriaged(conn)
    team = {r["id"] for r in rows if r["is_team"]}
    preds: dict[str, Prediction] = {}
    for state in json.loads(build_batch_input(conn, rows))["messages"]:
        mid = state["message_id"]
        try:
            res = llm.classify(state).result
        except BudgetExceeded:
            break
        except LLMError:
            continue
        if mid in team:  # production overrides staff messages the same way
            preds[mid] = Prediction(0, "other", False)
        else:
            preds[mid] = Prediction(res.sentiment, res.kind, res.needs_reply_p >= cfg.needs_reply_threshold)
    return preds, _cost(conn), _notes(conn)


def default_specs(config: Config) -> list[str]:
    specs = [str(config.models["triage"])]
    if config.classifier is not None and config.classifier.enabled:
        specs += [str(config.classifier.model), HYBRID]
    return specs


def _parse(spec: str) -> ModelRef | None:
    if spec == HYBRID:
        return None
    return ModelRef.parse(spec, PROVIDERS + CLASSIFIER_PROVIDERS)


def keys_for_specs(specs: list[str], config: Config, env: Mapping[str, str]) -> list[str]:
    needed: set[str] = set()
    for spec in specs:
        ref = _parse(spec)
        if ref is None:
            needed |= {KEY_ENV[config.models["triage"].provider], KEY_ENV["jev"]}
        else:
            needed.add(KEY_ENV[ref.provider])
    return sorted(v for v in needed if not env.get(v))


def run_eval(
    gold: GoldSet,
    config: Config,
    specs: list[str],
    *,
    backends_for: Callable[[Config], Mapping[str, Backend]],
    classifier_for: Callable[[], Classifier],
    max_usd: float,
    now: Callable[[], datetime] | None = None,
) -> list[EvalResult]:
    """Score each spec on the gold rows. Each run gets its own in-memory database and cap."""
    results = []
    for spec in specs:
        ref = _parse(spec)
        if ref is None:
            if config.classifier is None:
                raise ConfigError("hybrid needs a [classifier] section in pulse.toml")
            preds, cost, notes = _eval_triage(
                gold.rows, config, backends_for(_triage_only(config)), classifier=classifier_for(),
                classifier_cfg=replace(config.classifier, enabled=True), max_usd=max_usd, now=now,
            )
        elif ref.provider in CLASSIFIER_PROVIDERS:
            base = config.classifier or ClassifierConfig(enabled=True, model=ref)
            preds, cost, notes = _eval_jev(
                gold.rows, config, classifier_for(), replace(base, enabled=True, model=ref), max_usd=max_usd, now=now,
            )
        else:
            if ref.provider != "openrouter" and str(ref) not in config.pricing:
                raise ConfigError(f'[pricing."{ref}"] is required to evaluate {ref}')
            run_config = replace(config, models={**config.models, "triage": ref})
            preds, cost, notes = _eval_triage(
                gold.rows, run_config, backends_for(_triage_only(run_config)), max_usd=max_usd, now=now,
            )
        results.append(EvalResult(spec, score(gold.rows, preds), cost, notes))
    return results


def gate_lines(results: list[EvalResult], threshold: float = GATE) -> list[str]:
    lines = []
    for r in results:
        if not r.spec.startswith("jev:"):
            continue
        a = r.scores.needs_reply_agreement
        if a < threshold:
            lines.append(
                f"{r.spec}: needs-reply agreement {a:.0%} is below the {threshold:.0%} gate. "
                "Recommend setting [classifier] enabled = false."
            )
        else:
            lines.append(f"{r.spec}: needs-reply agreement {a:.0%} meets the {threshold:.0%} gate.")
    return lines


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def format_results(gold: GoldSet, results: list[EvalResult]) -> str:
    head = f"{len(gold.rows)} labelled messages"
    if gold.unlabeled:
        head += f", {gold.unlabeled} not yet labelled (skipped)"
    if gold.errors:
        head += f", {len(gold.errors)} bad lines (skipped)"
    cols = ("model", "sentiment", "±1", "kind F1", "reply recall", "reply precision", "reply agreement", "missing", "cost")
    table = [cols] + [
        (r.spec, _pct(r.scores.sentiment_exact), _pct(r.scores.sentiment_within_1), f"{r.scores.kind_macro_f1:.2f}",
         _pct(r.scores.needs_reply_recall), _pct(r.scores.needs_reply_precision),
         _pct(r.scores.needs_reply_agreement), str(r.scores.missing), f"${r.cost_usd:.4f}")
        for r in results
    ]
    widths = [max(len(row[i]) for row in table) for i in range(len(cols))]
    lines = [head, ""] + ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
    lines += [f"{r.spec}: {note}" for r in results for note in r.notes]
    gates = gate_lines(results)
    if gates:
        lines += [""] + gates
    if gold.errors:
        lines += ["", "Skipped lines:"] + [f"  {e}" for e in gold.errors[:20]]
    return "\n".join(lines)


def write_sample(conn: sqlite3.Connection, out: Path, n: int, *, seed: int = 0) -> int:
    """Write n random community messages with empty labels, for hand labelling."""
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; pick another --out")
    rows = conn.execute("SELECT * FROM messages WHERE is_bot = 0 AND trim(content) != '' ORDER BY id").fetchall()
    picked = random.Random(seed).sample(rows, min(n, len(rows)))
    picked.sort(key=lambda r: (r["created_at"], r["id"]))
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for r in picked:
            fh.write(json.dumps({
                "message_id": r["id"], "guild_id": r["guild_id"], "channel_id": r["channel_id"],
                "channel_name": r["channel_name"], "thread_id": r["thread_id"],
                "parent_channel_id": r["parent_channel_id"], "author_id": r["author_id"],
                "author_name": r["author_name"], "is_team": bool(r["is_team"]), "created_at": r["created_at"],
                "content": r["content"], "reply_to_id": r["reply_to_id"],
                "labels": {"sentiment": None, "kind": None, "needs_reply": None},
            }, ensure_ascii=False) + "\n")
    return len(picked)


def _jev() -> Classifier:
    from pulse.agents.providers.jev_backend import JevBackend

    return JevBackend()


def main(argv=None, *, env=None, backends_for=None, classifier_for=None) -> int:
    p = argparse.ArgumentParser(prog="pulse.eval", description="Score triage models against a hand-labelled gold set.")
    p.add_argument("--config", default="pulse.toml")
    p.add_argument("--gold", default="eval/gold.jsonl")
    p.add_argument("--model", action="append", dest="models", metavar="SPEC",
                   help="provider:model, jev:<model>, or hybrid; repeat to compare (default: what pulse.toml uses)")
    p.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD, help="spending cap for each model's run")
    p.add_argument("--json", type=Path, help="also write the results as JSON")
    p.add_argument("--sample", type=int, metavar="N", help="write N messages from the database to --out for labelling")
    p.add_argument("--out", type=Path, default=Path("eval/gold.jsonl"))
    args = p.parse_args(argv)
    env = os.environ if env is None else env
    try:
        config = load_config(args.config, env, require_keys=False)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    if args.sample is not None:
        conn = connect(config.db_path)
        try:
            written = write_sample(conn, args.out, args.sample)
        except FileExistsError as e:
            print(f"eval: {e}", file=sys.stderr)
            return 2
        finally:
            conn.close()
        print(f"wrote {written} messages to {args.out}; fill in each labels object, "
              f"then run python -m pulse.eval --gold {args.out}")
        return 0
    try:
        gold = load_gold(args.gold)
    except FileNotFoundError:
        print(f"eval: {args.gold} not found. Start one with --sample 200, "
              "or try --gold eval/gold.example.jsonl", file=sys.stderr)
        return 2
    if not gold.rows:
        print(f"eval: {args.gold} has no labelled rows", file=sys.stderr)
        return 2
    specs = args.models or default_specs(config)
    try:
        for spec in specs:
            _parse(spec)
        missing = keys_for_specs(specs, config, env)
        if missing:
            raise ConfigError(f"{', '.join(missing)} must be set to evaluate {', '.join(specs)}")
        results = run_eval(
            gold, config, specs, backends_for=backends_for or build_backends,
            classifier_for=classifier_for or _jev, max_usd=args.max_usd,
        )
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    print(format_results(gold, results))
    if args.json:
        args.json.write_text(json.dumps(
            [{"model": r.spec, "cost_usd": r.cost_usd, "notes": list(r.notes), **asdict(r.scores)} for r in results],
            indent=2,
        ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Add to `README.md` after the "Themes, digests, investigations" section:

````markdown
## Accuracy check

`python -m pulse.eval` runs triage on a hand-labelled set and reports, per model: exact sentiment, sentiment within one step, kind macro-F1, needs-reply recall, precision and agreement, messages left unlabelled by failures, and cost. It uses a throwaway in-memory database, so `pulse.db` is never touched, and `--max-usd` (default $1) caps each model's run.

```bash
.venv/bin/python -m pulse.eval --gold eval/gold.example.jsonl                          # the shipped synthetic set
.venv/bin/python -m pulse.eval --sample 200 --out eval/gold.jsonl                      # start your own set
.venv/bin/python -m pulse.eval --model anthropic:claude-haiku-4-5-20251001 --model openai:gpt-5.4-mini --model jev:jev-latest --model hybrid
```

`--sample` writes random messages from your database with empty labels; fill in `sentiment` (-2 to 2), `kind` and `needs_reply` for each, then run the check. Rows with a `null` label are skipped and counted. Run it before changing a prompt or provider. With the classifier enabled, any `jev:` row below 90% needs-reply agreement prints a recommendation to set `[classifier] enabled = false`.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_eval_run.py tests/test_config.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/eval.py pulse/config.py README.md tests/test_eval_run.py tests/test_config.py
git commit -m "feat: python -m pulse.eval with per-model scores, the Jev gate and --sample

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Bot backfill behind the Source interface, and `ingest --source bot`

**Files:**
- Create: `pulse/sources/bot_source.py`, `pulse/sources/bot_client.py`
- Modify: `pulse/config.py` (`Config.bot_backfill_days`), `pulse/run.py` (`ingest --source`), `pyproject.toml` (`bot` extra)
- Test: `tests/test_bot_source.py`, `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: `pulse.models.Message`, `from_iso`; `pulse.pipeline.ingest(conn, config, source)` (already filters by `channel_ids`, including threads of listed channels); `pulse.store.upsert_messages`.
- Produces (in `bot_source.py`, no discord import): `KEEP_TYPES = ("default", "reply")`, `BOT_PERMISSIONS = 66560`, `invite_url(client_id) -> str`, `message_from_discord(msg, guild_id) -> Message | None`, `wanted(config, channel_id, parent_id=None) -> bool`, `last_seen(conn) -> dict[str, datetime]` (latest stored `created_at` per `channel_id`; a thread's id is its channel id), `async backfill(guild, config, since, default_since, errors) -> list[Message]`, `class BotSource(conn, config, *, token, runner=None, now=None)` with `errors` and `fetch(since=None)`. In `bot_client.py`: `intents()`, `run_client(client, token, errors)`, `run_backfill(token, config, since, default_since, errors) -> list[Message]`. `Config.bot_backfill_days: int = 30` from `[bot] backfill_days`.

discord.py objects are duck-typed: a message has `.id .type.name .channel .author .content .created_at .edited_at .reference .guild`; an author `.id .name .display_name .bot .display_avatar.url`; a text channel `.id .name .history(after=, oldest_first=, limit=) .threads .archived_threads(limit=)`; a thread is a channel with `.parent_id`; a forum channel has `.threads` and `.archived_threads` but no `.history`; a guild `.id .name .text_channels .forums`. A thread's messages map exactly as FileSource maps a thread export: `channel_id` = thread id, `thread_id` = thread id, `parent_channel_id` = parent channel id.

- [ ] **Step 1: Write the failing tests**

`tests/test_bot_source.py`:

```python
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from pulse.db import connect
from pulse.pipeline import ingest
from pulse.sources.bot_source import (
    BOT_PERMISSIONS, BotSource, backfill, invite_url, last_seen, message_from_discord, wanted,
)
from pulse.store import upsert_messages
from tests.fakes import make_config, msg

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
GUILD = NS(id=900, name="Acme")


class Forbidden(Exception):
    pass


class FakeChannel:
    def __init__(self, id, name, *, fail=None, threads=(), archived=(), parent_id=None):
        self.id, self.name, self.parent_id = id, name, parent_id
        self.messages, self.threads, self.archived, self.fail = [], list(threads), list(archived), fail
        self.history_calls = []

    async def history(self, *, after=None, oldest_first=True, limit=None):
        self.history_calls.append(after)
        if self.fail:
            raise self.fail
        for m in sorted(self.messages, key=lambda m: m.created_at):
            if after is None or m.created_at > after:
                yield m

    async def archived_threads(self, *, limit=None):
        for t in self.archived:
            yield t


class FakeForum:
    def __init__(self, id, name, threads=()):
        self.id, self.name, self.threads = id, name, list(threads)

    async def archived_threads(self, *, limit=None):
        return
        yield


def post(channel, id, content="hi", *, minutes=0, author_id=1, kind="default", ref=None, bot=False, naive=False):
    created = T0 + timedelta(minutes=minutes)
    m = NS(
        id=id, type=NS(name=kind), channel=channel, guild=GUILD, content=content,
        created_at=created.replace(tzinfo=None) if naive else created, edited_at=None,
        reference=NS(message_id=ref) if ref else None,
        author=NS(id=author_id, name=f"user{author_id}", display_name=f"User {author_id}", bot=bot,
                  display_avatar=NS(url=f"https://cdn/{author_id}.png")),
    )
    channel.messages.append(m)
    return m


def test_message_mapping_matches_file_import():
    help_ = FakeChannel(100, "help")
    thread = FakeChannel(300, "M1 install", parent_id=100)
    m = message_from_discord(post(help_, 1, "install fails", ref=7, naive=True), "900")
    assert (m.id, m.guild_id, m.channel_id, m.channel_name, m.thread_id, m.parent_channel_id) == \
        ("1", "900", "100", "help", None, None)
    assert m.author_name == "User 1" and m.author_avatar_url == "https://cdn/1.png"
    assert m.reply_to_id == "7" and m.source == "bot" and m.created_at.tzinfo is not None
    t = message_from_discord(post(thread, 2, "same here", bot=True), "900")
    assert (t.channel_id, t.thread_id, t.parent_channel_id, t.is_bot) == ("300", "300", "100", True)
    assert message_from_discord(post(help_, 3, kind="pins_add"), "900") is None
    blank = post(help_, 4)
    blank.content = None
    assert message_from_discord(blank, "900").content == ""


def test_wanted_respects_channel_ids_and_thread_parents():
    assert wanted(make_config(), "5")
    cfg = make_config(channel_ids=("100",))
    assert wanted(cfg, "100") and wanted(cfg, "300", "100")
    assert not wanted(cfg, "200") and not wanted(cfg, "300", "200")


def guild_with_everything():
    help_ = FakeChannel(100, "help")
    old = post(help_, 1, "old", minutes=0)
    post(help_, 2, "new", minutes=10)
    active = FakeChannel(300, "M1 install", parent_id=100)
    post(active, 3, "in thread", minutes=5)
    archived = FakeChannel(301, "old thread", parent_id=100)
    post(archived, 4, "archived reply", minutes=6)
    help_.threads, help_.archived = [active], [archived, active]
    general = FakeChannel(200, "general")
    post(general, 5, "chit chat", minutes=1)
    forum_thread = FakeChannel(401, "How do I deploy?", parent_id=400)
    post(forum_thread, 6, "forum question", minutes=2)
    forum = FakeForum(400, "help-forum", threads=[forum_thread])
    guild = NS(id=900, name="Acme", text_channels=[help_, general], forums=[forum])
    return guild, help_, general, old


def test_backfill_reads_channels_threads_and_forums_since_last_seen():
    guild, help_, general, old = guild_with_everything()
    errors = []
    since = {"100": old.created_at}
    out = asyncio.run(backfill(guild, make_config(), since, T0 - timedelta(days=30), errors))
    assert errors == []
    assert sorted(m.id for m in out) == ["2", "3", "4", "5", "6"]
    assert help_.history_calls == [old.created_at]
    assert general.history_calls == [T0 - timedelta(days=30)]
    assert [m.parent_channel_id for m in out if m.id == "6"] == ["400"]


def test_backfill_honours_channel_ids():
    guild, help_, general, _ = guild_with_everything()
    out = asyncio.run(backfill(guild, make_config(channel_ids=("100",)), {}, T0 - timedelta(days=1), []))
    assert sorted(m.id for m in out) == ["1", "2", "3", "4"]
    assert general.history_calls == []


def test_backfill_continues_past_unreadable_channel():
    secret = FakeChannel(150, "secret", fail=Forbidden("Missing Access"))
    help_ = FakeChannel(100, "help")
    post(help_, 1, "readable")
    guild = NS(id=900, name="Acme", text_channels=[secret, help_], forums=[])
    errors = []
    out = asyncio.run(backfill(guild, make_config(), {}, T0 - timedelta(days=1), errors))
    assert [m.id for m in out] == ["1"]
    assert errors == ["#secret: Forbidden: Missing Access"]


def test_bot_source_fetch_passes_last_seen_and_default_window():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("a", minutes=0), msg("b", minutes=5)], frozenset())
    seen = {}

    def runner(token, config, since, default_since, errors):
        seen.update(token=token, since=since, default_since=default_since)
        errors.append("#secret: Forbidden: Missing Access")
        return [message_from_discord(post(FakeChannel(100, "help"), 9, "live", minutes=20), "900")]

    source = BotSource(conn, make_config(), token="tok", runner=runner, now=lambda: T0)
    stats, errors = ingest(conn, make_config(), source)
    assert seen["token"] == "tok" and seen["default_since"] == T0 - timedelta(days=30)
    assert seen["since"] == {"100": T0 + timedelta(minutes=5)}
    assert stats.inserted == 1 and errors == ["#secret: Forbidden: Missing Access"]
    assert conn.execute("SELECT source FROM messages WHERE id = '9'").fetchone()["source"] == "bot"
    assert last_seen(conn)["100"] == T0 + timedelta(minutes=20)


def test_invite_url_requests_read_only_permissions():
    assert BOT_PERMISSIONS == 66560
    assert invite_url("123") == "https://discord.com/oauth2/authorize?client_id=123&scope=bot&permissions=66560"
```

Append to `tests/test_cli.py`:

```python
def test_ingest_from_bot_needs_the_extra_and_a_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    (tmp_path / "pulse.toml").write_text(CONFIG)
    cfg = str(tmp_path / "pulse.toml")
    monkeypatch.setattr("pulse.run._discord_installed", lambda: False)
    assert main(["--config", cfg, "ingest", "--source", "bot"]) == 2
    assert "pip install -e '.[bot]'" in capsys.readouterr().err
    monkeypatch.setattr("pulse.run._discord_installed", lambda: True)
    assert main(["--config", cfg, "ingest", "--source", "bot"]) == 2
    assert "DISCORD_BOT_TOKEN must be set" in capsys.readouterr().err


def test_bot_backfill_days_config(tmp_path, monkeypatch):
    from pulse.config import ConfigError, load_config

    path = tmp_path / "pulse.toml"
    path.write_text(CONFIG + "\n[bot]\nbackfill_days = 7\n")
    assert load_config(path, {"ANTHROPIC_API_KEY": "x"}).bot_backfill_days == 7
    path.write_text(CONFIG + "\n[bot]\nbackfill_days = 0\n")
    with pytest.raises(ConfigError, match="backfill_days"):
        load_config(path, {"ANTHROPIC_API_KEY": "x"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_bot_source.py tests/test_cli.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.sources.bot_source'`

- [ ] **Step 3: Write the implementation**

`pulse/sources/bot_source.py`:

```python
"""Read-only Discord bot ingest, free of discord.py so it is tested with fakes.

The bot only reads: it never posts, reacts, edits or deletes. bot_client.py holds the
discord.py glue and is the only module that imports discord.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from pulse.config import Config
from pulse.models import Message, from_iso

KEEP_TYPES = ("default", "reply")
BOT_PERMISSIONS = 1024 | 65536  # View Channels + Read Message History
_INVITE = "https://discord.com/oauth2/authorize?client_id={client_id}&scope=bot&permissions={permissions}"


def invite_url(client_id: str) -> str:
    return _INVITE.format(client_id=client_id, permissions=BOT_PERMISSIONS)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def message_from_discord(msg, guild_id: str) -> Message | None:
    """Map a discord.py message to ours; None for system messages (pins, joins, boosts)."""
    if getattr(msg.type, "name", str(msg.type)) not in KEEP_TYPES:
        return None
    channel, author = msg.channel, msg.author
    parent_id = getattr(channel, "parent_id", None)
    is_thread = parent_id is not None
    avatar = getattr(getattr(author, "display_avatar", None), "url", None)
    reference = getattr(msg, "reference", None)
    reply_to = getattr(reference, "message_id", None) if reference is not None else None
    return Message(
        id=str(msg.id),
        guild_id=guild_id,
        channel_id=str(channel.id),
        channel_name=str(getattr(channel, "name", "") or ""),
        thread_id=str(channel.id) if is_thread else None,
        parent_channel_id=str(parent_id) if is_thread else None,
        author_id=str(author.id),
        author_name=str(getattr(author, "display_name", None) or author.name),
        author_avatar_url=str(avatar) if avatar else None,
        content=msg.content or "",
        created_at=_aware(msg.created_at),
        edited_at=_aware(getattr(msg, "edited_at", None)),
        reply_to_id=str(reply_to) if reply_to else None,
        is_bot=bool(getattr(author, "bot", False)),
        source="bot",
    )


def wanted(config: Config, channel_id: str, parent_id: str | None = None) -> bool:
    ids = set(config.channel_ids)
    return not ids or channel_id in ids or (parent_id is not None and parent_id in ids)


def last_seen(conn: sqlite3.Connection) -> dict[str, datetime]:
    """Latest stored message time per channel (a thread's id is its channel id)."""
    return {
        r["channel_id"]: from_iso(r["latest"])
        for r in conn.execute("SELECT channel_id, MAX(created_at) AS latest FROM messages GROUP BY channel_id")
    }


async def _history(target, after: datetime, guild_id: str, out: list[Message], errors: list[str], label: str) -> None:
    try:
        async for raw in target.history(after=after, oldest_first=True, limit=None):
            m = message_from_discord(raw, guild_id)
            if m is not None:
                out.append(m)
    except Exception as e:  # Forbidden, NotFound, HTTPException: one channel never stops the rest
        errors.append(f"{label}: {type(e).__name__}: {e}")


async def _threads(channel, errors: list[str], label: str) -> list:
    threads = list(getattr(channel, "threads", None) or [])
    archived = getattr(channel, "archived_threads", None)
    if archived is not None:
        try:
            async for t in archived(limit=None):
                threads.append(t)
        except Exception as e:
            errors.append(f"{label} archived threads: {type(e).__name__}: {e}")
    unique, seen = [], set()
    for t in threads:
        if t.id not in seen:
            seen.add(t.id)
            unique.append(t)
    return unique


async def backfill(
    guild, config: Config, since: dict[str, datetime], default_since: datetime, errors: list[str]
) -> list[Message]:
    """Messages after the last stored one in each wanted channel, its threads and forum threads.
    Private threads the bot cannot see are simply absent."""
    guild_id = str(guild.id)
    out: list[Message] = []
    for channel in list(getattr(guild, "text_channels", [])) + list(getattr(guild, "forums", [])):
        cid = str(channel.id)
        if not wanted(config, cid):
            continue
        label = f"#{channel.name}"
        if hasattr(channel, "history"):  # forum channels hold only threads
            await _history(channel, since.get(cid, default_since), guild_id, out, errors, label)
        for thread in await _threads(channel, errors, label):
            tid = str(thread.id)
            await _history(thread, since.get(tid, default_since), guild_id, out, errors, f"{label} › {thread.name}")
    return out


Runner = Callable[[str, Config, dict, datetime, list], list[Message]]


def _default_runner(token, config, since, default_since, errors) -> list[Message]:
    from pulse.sources.bot_client import run_backfill

    return run_backfill(token, config, since, default_since, errors)


class BotSource:
    """One-shot backfill through the bot, behind the same interface as FileSource.

    The backfill completes before the first message is yielded, so `errors` is final
    once the iterator is exhausted, as with FileSource.
    """

    def __init__(self, conn: sqlite3.Connection, config: Config, *, token: str,
                 runner: Runner | None = None, now: Callable[[], datetime] | None = None):
        self._conn, self._config, self._token = conn, config, token
        self._runner = runner or _default_runner
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.errors: list[str] = []

    def fetch(self, since: datetime | None = None) -> Iterator[Message]:
        self.errors = []
        default_since = since or (self._now() - timedelta(days=self._config.bot_backfill_days))
        yield from self._runner(self._token, self._config, last_seen(self._conn), default_since, self.errors)
```

`pulse/sources/bot_client.py`:

```python
"""discord.py glue for the read-only bot. The only module that imports discord."""
from __future__ import annotations

from datetime import datetime

from pulse.config import Config
from pulse.models import Message
from pulse.sources.bot_source import backfill


def intents():
    import discord

    i = discord.Intents.none()
    i.guilds = True
    i.guild_messages = True
    i.message_content = True
    return i


def run_client(client, token: str, errors: list[str]) -> None:
    import discord

    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        errors.append("Discord rejected DISCORD_BOT_TOKEN")
    except discord.PrivilegedIntentsRequired:
        errors.append("turn on Message Content Intent on the bot's page in the Discord Developer Portal")


def run_backfill(token: str, config: Config, since: dict, default_since: datetime, errors: list[str]) -> list[Message]:
    import discord

    client = discord.Client(intents=intents())
    out: list[Message] = []

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(config.guild_id))
            if guild is None:
                errors.append(f"the bot is not in server {config.guild_id}; see python -m pulse.run bot-invite")
            else:
                out.extend(await backfill(guild, config, since, default_since, errors))
        finally:
            await client.close()

    run_client(client, token, errors)
    return out
```

In `pulse/config.py`, add `bot_backfill_days: int = 30` as the last `Config` field, and in `load_config` before the `return`:

```python
    backfill_days = raw.get("bot", {}).get("backfill_days", 30)
    if isinstance(backfill_days, bool) or not isinstance(backfill_days, int) or backfill_days < 1:
        raise ConfigError(f"[bot] backfill_days must be a whole number of days, at least 1, got {backfill_days!r}")
```

and pass `bot_backfill_days=backfill_days` in the `Config(...)` call.

In `pulse/run.py`: add `import importlib.util` and `import os` at the top, `from pulse.sources.bot_source import BotSource`, then:

```python
def _discord_installed() -> bool:
    return importlib.util.find_spec("discord") is not None


def _bot_token() -> str | None:
    """The bot token, or None after printing why the bot cannot run."""
    if not _discord_installed():
        print("bot: install the bot extra first: .venv/bin/pip install -e '.[bot]'", file=sys.stderr)
        return None
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("bot: DISCORD_BOT_TOKEN must be set (see docs/bot-pitch.md for setup)", file=sys.stderr)
        return None
    return token
```

Change the `ingest` parser line to:

```python
    ingest_p = sub.add_parser("ingest", help="import messages from export files, or backfill through the bot")
    ingest_p.add_argument("--source", choices=("file", "bot"), default="file")
```

and the `ingest` branch of `main` to:

```python
    if args.command == "ingest":
        if args.source == "bot":
            token = _bot_token()
            if token is None:
                return 2
            source = BotSource(conn, config, token=token)
        else:
            source = FileSource(config.imports_dir)
        stats, errors = ingest(conn, config, source)
        print(format_ingest(stats, errors))
```

In `pyproject.toml`, add under `[project.optional-dependencies]`: `bot = ["discord.py>=2.4"]`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_bot_source.py tests/test_cli.py -q` then `.venv/bin/pytest -q`
Expected: all pass, and `python -c "import pulse.sources.bot_source, pulse.sources.bot_client"` works without discord.py installed.

- [ ] **Step 5: Commit**

```bash
git add pulse/sources/bot_source.py pulse/sources/bot_client.py pulse/config.py pulse/run.py pyproject.toml tests/test_bot_source.py tests/test_cli.py
git commit -m "feat: read-only bot backfill behind the Source interface (ingest --source bot)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Live bot streamer, `bot` and `bot-invite` commands, and the mod-team pitch

**Files:**
- Modify: `pulse/sources/bot_source.py` (append `BotStreamer`), `pulse/sources/bot_client.py` (append `run_streamer`), `pulse/run.py`, `README.md`, `pulse.toml.example`
- Create: `docs/bot-pitch.md`
- Test: `tests/test_bot_streamer.py`

**Interfaces:**
- Consumes: Task 3 (`message_from_discord`, `wanted`, `last_seen`, `backfill`, `invite_url`, `intents`, `run_client`, `_bot_token`).
- Produces: `class BotStreamer(conn, config)` with `add(msg) -> bool`, `add_many(messages)`, `flush() -> int`, `since() -> dict[str, datetime]`, counters `received`, `written`, `flush_failures`; `FLUSH_SECONDS = 5`; `run_streamer(token, config, conn, *, log=print) -> BotStreamer`; CLI `bot` and `bot-invite --client-id ID`.

`add` runs on the event loop and `flush` in a worker thread, so the pending buffer is guarded by a lock and database access by a second lock. A flush that fails keeps its messages; a newer version of a message that arrived meanwhile wins.

- [ ] **Step 1: Write the failing tests**

`tests/test_bot_streamer.py`:

```python
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from pulse.db import connect
from pulse.run import main
from pulse.sources import bot_source
from pulse.sources.bot_source import BotStreamer
from tests.fakes import make_config

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
HELP = NS(id=100, name="help")
GENERAL = NS(id=200, name="general")


def live(id, content="hi", *, channel=HELP, guild_id=900, kind="default", minutes=0):
    return NS(
        id=id, type=NS(name=kind), channel=channel, guild=NS(id=guild_id) if guild_id else None,
        content=content, created_at=T0 + timedelta(minutes=minutes), edited_at=None, reference=None,
        author=NS(id=1, name="alice", display_name="Alice", bot=False, display_avatar=None),
    )


def stored(conn):
    return {r["id"]: r["content"] for r in conn.execute("SELECT id, content FROM messages")}


def test_add_keeps_only_wanted_messages_from_this_server():
    s = BotStreamer(connect(":memory:"), make_config(channel_ids=("100",)))
    assert s.add(live(1))
    assert not s.add(live(2, guild_id=999))
    assert not s.add(live(3, guild_id=None))
    assert not s.add(live(4, kind="pins_add"))
    assert not s.add(live(5, channel=GENERAL))
    assert s.received == 1


def test_flush_writes_latest_version_once():
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    s.add(live(1, "first"))
    s.add(live(1, "edited"))
    s.add(live(2, "second"))
    assert s.flush() == 2 and s.written == 2
    assert stored(conn) == {"1": "edited", "2": "second"}
    assert s.flush() == 0


def test_flush_keeps_messages_when_database_is_locked(monkeypatch):
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    real = bot_source.upsert_messages
    calls = []

    def locked_once(*args):
        calls.append(1)
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(*args)

    monkeypatch.setattr(bot_source, "upsert_messages", locked_once)
    s.add(live(1, "first"))
    assert s.flush() == 0 and s.flush_failures == 1 and stored(conn) == {}
    s.add(live(1, "edited while locked"))
    assert s.flush() == 1
    assert stored(conn) == {"1": "edited while locked"}


def test_other_database_errors_propagate_but_keep_the_batch(monkeypatch):
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    monkeypatch.setattr(bot_source, "upsert_messages",
                        lambda *a: (_ for _ in ()).throw(sqlite3.OperationalError("disk I/O error")))
    s.add(live(1))
    with pytest.raises(sqlite3.OperationalError):
        s.flush()
    monkeypatch.undo()
    assert s.flush() == 1


def test_add_many_and_since():
    conn = connect(":memory:")
    s = BotStreamer(conn, make_config())
    s.add_many([bot_source.message_from_discord(live(7, minutes=3), "900")])
    s.flush()
    assert s.since() == {"100": T0 + timedelta(minutes=3)}


def test_bot_invite_prints_the_read_only_link(capsys):
    assert main(["bot-invite", "--client-id", "123456"]) == 0
    out = capsys.readouterr().out
    assert "client_id=123456&scope=bot&permissions=66560" in out
    with pytest.raises(SystemExit):
        main(["bot-invite", "--client-id", "abc"])


def test_bot_command_without_token_exits_2(tmp_path, monkeypatch, capsys):
    from tests.test_cli import CONFIG

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr("pulse.run._discord_installed", lambda: True)
    (tmp_path / "pulse.toml").write_text(CONFIG)
    assert main(["--config", str(tmp_path / "pulse.toml"), "bot"]) == 2
    assert "DISCORD_BOT_TOKEN must be set" in capsys.readouterr().err


def test_pitch_doc_states_the_exact_access():
    pitch = Path("docs/bot-pitch.md").read_text()
    for phrase in ("View Channels", "Read Message History", "Message Content Intent", "never posts",
                   "bot-invite", "How to remove it"):
        assert phrase in pitch
    readme = Path("README.md").read_text()
    assert "ingest --source bot" in readme and "docs/bot-pitch.md" in readme
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_bot_streamer.py -q`
Expected: FAIL with `ImportError: cannot import name 'BotStreamer'`

- [ ] **Step 3: Write the implementation**

Append to `pulse/sources/bot_source.py` (add `import threading` and `from pulse.store import upsert_messages` to the imports):

```python
FLUSH_SECONDS = 5


class BotStreamer:
    """Buffers live messages and writes them in batches.

    add() runs on the event loop; flush() runs in a worker thread. A flush that hits
    "database is locked" (the pipeline is writing) keeps its batch for the next flush.
    """

    def __init__(self, conn: sqlite3.Connection, config: Config):
        self._conn, self._config = conn, config
        self._pending: dict[str, Message] = {}
        self._pending_lock = threading.Lock()
        self._db_lock = threading.Lock()
        self.received = self.written = self.flush_failures = 0

    def add(self, msg) -> bool:
        guild = getattr(msg, "guild", None)
        if guild is None or str(guild.id) != self._config.guild_id:
            return False
        m = message_from_discord(msg, self._config.guild_id)
        if m is None or not wanted(self._config, m.channel_id, m.parent_channel_id):
            return False
        with self._pending_lock:
            self._pending[m.id] = m
            self.received += 1
        return True

    def add_many(self, messages: list[Message]) -> None:
        with self._pending_lock:
            for m in messages:
                self._pending[m.id] = m

    def since(self) -> dict[str, datetime]:
        with self._db_lock:
            return last_seen(self._conn)

    def flush(self) -> int:
        with self._pending_lock:
            batch, self._pending = self._pending, {}
        if not batch:
            return 0
        try:
            with self._db_lock:
                upsert_messages(self._conn, list(batch.values()), self._config.team_member_ids)
        except sqlite3.OperationalError as e:
            with self._pending_lock:
                for mid, m in batch.items():
                    self._pending.setdefault(mid, m)  # a newer version that arrived meanwhile wins
            self.flush_failures += 1
            if "locked" in str(e):
                return 0
            raise
        self.written += len(batch)
        return len(batch)
```

Append to `pulse/sources/bot_client.py` (add `import asyncio`, `from datetime import timedelta, timezone`, and `from pulse.sources.bot_source import FLUSH_SECONDS, BotStreamer` to the imports):

```python
def run_streamer(token: str, config: Config, conn, *, log=print) -> BotStreamer:
    """Connect, catch up on every (re)connect, then stream new and edited messages until stopped."""
    import discord

    client = discord.Client(intents=intents())
    streamer = BotStreamer(conn, config)
    errors: list[str] = []
    state = {"flusher": None}

    async def flush_forever():
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await asyncio.to_thread(streamer.flush)
            except Exception as e:
                log(f"bot: flush failed, will retry: {type(e).__name__}: {e}")

    @client.event
    async def on_ready():
        guild = client.get_guild(int(config.guild_id))
        if guild is None:
            log(f"bot: not in server {config.guild_id}; see python -m pulse.run bot-invite")
            await client.close()
            return
        # on_ready fires again after a reconnect, so this also catches up on anything missed.
        default_since = datetime.now(timezone.utc) - timedelta(days=config.bot_backfill_days)
        since = await asyncio.to_thread(streamer.since)
        caught_up = await backfill(guild, config, since, default_since, errors)
        streamer.add_many(caught_up)
        for e in errors:
            log(f"bot: {e}")
        errors.clear()
        log(f"bot: connected to {guild.name}; caught up on {len(caught_up)} messages; streaming")
        if state["flusher"] is None:
            state["flusher"] = asyncio.create_task(flush_forever())

    @client.event
    async def on_message(message):
        streamer.add(message)

    @client.event
    async def on_message_edit(before, after):
        streamer.add(after)

    run_client(client, token, errors)
    for e in errors:
        log(f"bot: {e}")
    streamer.flush()
    return streamer
```

In `pulse/run.py`: import `from pulse.sources.bot_source import BotSource, invite_url`; add parsers:

```python
    sub.add_parser("bot", help="run the read-only bot: catch up, then stream new messages (Ctrl-C to stop)")
    invite = sub.add_parser("bot-invite", help="print the invite link that asks only for read access")
    invite.add_argument("--client-id", required=True, help="the Application ID from the Discord Developer Portal")
```

In `main`, right after the `triage --force` check:

```python
    if args.command == "bot-invite":
        if not args.client_id.isdigit():
            parser.error("--client-id is the Application ID: digits only")
        print(invite_url(args.client_id))
        print("Asks for View Channels and Read Message History only. "
              "Open it as someone with Manage Server on the community server.")
        return 0
```

and a branch after `investigate`:

```python
    elif args.command == "bot":
        token = _bot_token()
        if token is None:
            return 2
        from pulse.sources.bot_client import run_streamer

        streamer = run_streamer(token, config, conn)
        print(f"bot stopped: {streamer.received} live messages received, {streamer.written} written")
```

Append to `pulse.toml.example`:

```toml
[bot]
# For a channel the bot has never read, how far back the first backfill goes.
backfill_days = 30
```

`docs/bot-pitch.md` (exact content):

```markdown
# Discord Pulse: a read-only bot for the community team

## What it is

Discord Pulse helps the community team see how people feel about the product and where they get stuck. It reads messages in the channels you allow and produces three things for the team: a sentiment trend, a ranked list of recurring pain points (bugs, confusing docs, setup trouble), and a queue of questions that have not had a staff reply.

## What the bot does and does not do

- It reads messages in the channels it can see.
- It never posts, replies, reacts, sends DMs, edits, deletes, pins, or changes any setting. It has no permission to do any of that.
- It does not read private channels or private threads unless you give it access to them.

## Exactly what access it asks for

- Scope: `bot`.
- Permissions: **View Channels** and **Read Message History**. Nothing else: no Administrator, no Send Messages, no Manage permissions.
- Privileged intent: **Message Content Intent**, which Discord requires for a bot to read message text.

You decide which channels it reads through normal channel permissions. The team can narrow it further in its own settings.

## Where the data goes

- Messages are stored in a database file on the community lead's computer. Nothing is hosted on a server.
- To label each message, its text is sent to the AI provider the team has configured (for example Anthropic or OpenAI). Those providers do not train on API data by default.
- The dashboard runs on that computer only and is not reachable from the internet.

## How to remove it

Kick the bot from the server. That ends all access immediately. On request, the community lead deletes the local database.

## Setup (about five minutes)

1. Whoever owns the bot application creates it in the Discord Developer Portal (Applications, New Application, Bot) and turns on **Message Content Intent** on the Bot page.
2. They run `python -m pulse.run bot-invite --client-id <Application ID>` and send you the link it prints. The link asks only for View Channels and Read Message History.
3. A member with Manage Server opens the link and picks this server.
4. Optional: limit which channels it can see using channel permissions.

Questions or concerns: ask the community lead before or after it is added; it can be removed at any time.
```

Add to `README.md` after the "Accuracy check" section:

````markdown
## Live bot

When the mod team has added the read-only bot (see `docs/bot-pitch.md` for the one-page pitch and setup), install the extra and set the token:

```bash
.venv/bin/pip install -e '.[bot]'
export DISCORD_BOT_TOKEN=...                                   # from the Developer Portal, never in pulse.toml
.venv/bin/python -m pulse.run bot-invite --client-id <app id>  # the read-only invite link
.venv/bin/python -m pulse.run ingest --source bot              # one-off catch-up, then exit
.venv/bin/python -m pulse.run bot                              # catch up, then stream until Ctrl-C
```

Both read only channels the bot can see, narrowed by `[server] channel_ids`. A channel read for the first time goes back `[bot] backfill_days` (default 30); after that each run continues from the last stored message, so a restart or disconnect never leaves a gap. Edits to messages the bot saw while running replace the old text and re-queue them for triage. `pipeline` and the dashboard work the same whether messages came from exports or the bot.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_bot_streamer.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/sources/bot_source.py pulse/sources/bot_client.py pulse/run.py README.md pulse.toml.example docs/bot-pitch.md tests/test_bot_streamer.py
git commit -m "feat: live read-only bot streamer, bot-invite, and the mod-team pitch

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Scheduled runs with launchd

**Files:**
- Create: `pulse/schedule.py`
- Modify: `pulse/run.py` (`schedule` command, handled before the config is loaded), `README.md`
- Test: `tests/test_schedule.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (the jobs call `python -m pulse.run pipeline`, `digest` and `bot`).
- Produces: `LABEL_PREFIX = "com.discordpulse"`, `DEFAULT_ENV_FILE`, `AGENTS_DIR`, `Job(name, command, interval_seconds=None, weekly=None, keep_alive=False)`, `JOBS` (`pipeline` every 1800 s, `digest` Mondays 09:00 local time, `bot` kept alive), `label(name)`, `build_plist(job, *, python, config, env_file, log_dir) -> dict`, `run_launchctl(cmd) -> int`, `ScheduleError`, `install(names, *, python, config, env_file, log_dir, agents_dir, runner, uid=None) -> list[Path]`, `uninstall(names, *, agents_dir, runner, uid=None) -> list[Path]`. CLI: `schedule install|uninstall|show [--with-bot] [--env-file PATH]`.

launchd does not inherit the shell's environment, and keys must never be written into a plist. Each job therefore runs `/bin/zsh -c` with a script that loads the env file (the one created during the first real-model run, `~/.config/discord-pulse/env`, by default) and then `exec`s the command. Paths are shell-quoted, so a project folder with spaces works.

- [ ] **Step 1: Write the failing tests**

`tests/test_schedule.py`:

```python
import plistlib
from pathlib import Path

import pytest

from pulse import schedule
from pulse.run import main
from pulse.schedule import JOBS, ScheduleError, build_plist, install, label, uninstall


def plist_for(name, tmp_path):
    return build_plist(
        JOBS[name], python=Path("/opt/py/bin/python"), config=tmp_path / "my dir" / "pulse.toml",
        env_file=tmp_path / "keys env", log_dir=tmp_path / "logs",
    )


def test_pipeline_plist_runs_every_30_minutes_and_sources_the_env_file(tmp_path):
    p = plist_for("pipeline", tmp_path)
    assert p["Label"] == "com.discordpulse.pipeline"
    assert p["StartInterval"] == 1800 and p["RunAtLoad"] is True
    assert p["WorkingDirectory"] == str(tmp_path / "my dir")
    assert p["StandardOutPath"] == p["StandardErrorPath"] == str(tmp_path / "logs" / "pipeline.log")
    shell, flag, script = p["ProgramArguments"]
    assert (shell, flag) == ("/bin/zsh", "-c")
    assert f"'{tmp_path / 'keys env'}'" in script
    assert script.endswith(f"exec /opt/py/bin/python -m pulse.run --config '{tmp_path / 'my dir' / 'pulse.toml'}' pipeline")
    plistlib.loads(plistlib.dumps(p))


def test_digest_is_weekly_and_bot_is_kept_alive(tmp_path):
    d = plist_for("digest", tmp_path)
    assert d["StartCalendarInterval"] == {"Weekday": 1, "Hour": 9, "Minute": 0} and "StartInterval" not in d
    b = plist_for("bot", tmp_path)
    assert b["KeepAlive"] is True and b["RunAtLoad"] is True and b["ThrottleInterval"] == 60


def test_install_writes_plists_and_bootstraps_without_secrets(tmp_path):
    env_file = tmp_path / "env"
    env_file.write_text("export ANTHROPIC_API_KEY=sk-secret-value\n")
    calls = []
    agents = tmp_path / "LaunchAgents"
    paths = install(
        ["pipeline", "digest"], python=Path("/py"), config=tmp_path / "pulse.toml", env_file=env_file,
        log_dir=tmp_path / "logs", agents_dir=agents, runner=lambda cmd: calls.append(cmd) or 0, uid=501,
    )
    assert paths == [agents / "com.discordpulse.pipeline.plist", agents / "com.discordpulse.digest.plist"]
    assert calls == [
        ["launchctl", "bootout", "gui/501/com.discordpulse.pipeline"],
        ["launchctl", "bootstrap", "gui/501", str(paths[0])],
        ["launchctl", "bootout", "gui/501/com.discordpulse.digest"],
        ["launchctl", "bootstrap", "gui/501", str(paths[1])],
    ]
    for p in paths:
        assert b"sk-secret-value" not in p.read_bytes()
    assert (tmp_path / "logs").is_dir()


def test_failed_bootstrap_raises(tmp_path):
    runner = lambda cmd: 5 if cmd[1] == "bootstrap" else 0
    with pytest.raises(ScheduleError, match="exit 5"):
        install(["pipeline"], python=Path("/py"), config=tmp_path / "pulse.toml", env_file=tmp_path / "env",
                log_dir=tmp_path / "logs", agents_dir=tmp_path / "A", runner=runner, uid=501)


def test_uninstall_boots_out_and_removes(tmp_path):
    agents = tmp_path / "A"
    agents.mkdir()
    (agents / f"{label('pipeline')}.plist").write_text("x")
    calls = []
    removed = uninstall(["pipeline", "digest", "bot"], agents_dir=agents,
                        runner=lambda cmd: calls.append(cmd) or 0, uid=501)
    assert removed == [agents / "com.discordpulse.pipeline.plist"]
    assert len(calls) == 3 and all(c[1] == "bootout" for c in calls)


def test_schedule_cli_show_install_uninstall(tmp_path, monkeypatch, capsys):
    (tmp_path / "pulse.toml").write_text("[server]\n")  # not loaded: schedule never needs keys
    calls = []
    monkeypatch.setattr(schedule, "AGENTS_DIR", tmp_path / "A")
    monkeypatch.setattr(schedule, "run_launchctl", lambda cmd: calls.append(cmd) or 0)
    cfg = str(tmp_path / "pulse.toml")
    assert main(["--config", cfg, "schedule", "show", "--with-bot"]) == 0
    out = capsys.readouterr().out
    assert out.count("<key>Label</key>") == 3
    assert main(["--config", cfg, "schedule", "install", "--env-file", str(tmp_path / "missing")]) == 0
    captured = capsys.readouterr()
    assert "com.discordpulse.pipeline.plist" in captured.out and "not found" in captured.err
    assert sorted(p.name for p in (tmp_path / "A").iterdir()) == [
        "com.discordpulse.digest.plist", "com.discordpulse.pipeline.plist"]
    assert main(["--config", cfg, "schedule", "uninstall"]) == 0
    assert list((tmp_path / "A").iterdir()) == []
    assert main(["--config", str(tmp_path / "nope.toml"), "schedule", "install"]) == 2
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_schedule.py -q`
Expected: FAIL with `ImportError: cannot import name 'schedule' from 'pulse'`

- [ ] **Step 3: Write the implementation**

`pulse/schedule.py`:

```python
"""launchd jobs on macOS: the pipeline every 30 minutes, a weekly digest, and optionally the bot.

API keys are never written into a plist. Each job loads an env file when it starts.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LABEL_PREFIX = "com.discordpulse"
DEFAULT_ENV_FILE = Path("~/.config/discord-pulse/env").expanduser()
AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class ScheduleError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    name: str
    command: str
    interval_seconds: int | None = None
    weekly: dict | None = None
    keep_alive: bool = False


JOBS = {
    "pipeline": Job("pipeline", "pipeline", interval_seconds=1800),
    "digest": Job("digest", "digest", weekly={"Weekday": 1, "Hour": 9, "Minute": 0}),
    "bot": Job("bot", "bot", keep_alive=True),
}


def label(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def build_plist(job: Job, *, python: Path, config: Path, env_file: Path, log_dir: Path) -> dict:
    env = shlex.quote(str(env_file))
    script = (
        f"set -a; [ -f {env} ] && . {env}; set +a; "
        f"exec {shlex.quote(str(python))} -m pulse.run --config {shlex.quote(str(config))} {job.command}"
    )
    log = str(log_dir / f"{job.name}.log")
    plist = {
        "Label": label(job.name),
        "ProgramArguments": ["/bin/zsh", "-c", script],
        "WorkingDirectory": str(config.parent),
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PATH": _PATH},
        "ProcessType": "Background",
    }
    if job.interval_seconds:
        plist["StartInterval"] = job.interval_seconds
        plist["RunAtLoad"] = True
    if job.weekly:
        plist["StartCalendarInterval"] = dict(job.weekly)
    if job.keep_alive:
        plist["KeepAlive"] = True
        plist["RunAtLoad"] = True
        plist["ThrottleInterval"] = 60
    return plist


def run_launchctl(cmd: list[str]) -> int:
    return subprocess.run(cmd, capture_output=True).returncode


def _path(agents_dir: Path, name: str) -> Path:
    return agents_dir / f"{label(name)}.plist"


def install(
    names: list[str], *, python: Path, config: Path, env_file: Path, log_dir: Path,
    agents_dir: Path, runner: Callable[[list[str]], int], uid: int | None = None,
) -> list[Path]:
    uid = os.getuid() if uid is None else uid
    agents_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in names:
        path = _path(agents_dir, name)
        path.write_bytes(plistlib.dumps(
            build_plist(JOBS[name], python=python, config=config, env_file=env_file, log_dir=log_dir)
        ))
        runner(["launchctl", "bootout", f"gui/{uid}/{label(name)}"])  # replaces an older copy; harmless if none
        code = runner(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        if code != 0:
            raise ScheduleError(f"launchctl bootstrap failed for {path} (exit {code})")
        written.append(path)
    return written


def uninstall(
    names: list[str], *, agents_dir: Path, runner: Callable[[list[str]], int], uid: int | None = None
) -> list[Path]:
    uid = os.getuid() if uid is None else uid
    removed = []
    for name in names:
        runner(["launchctl", "bootout", f"gui/{uid}/{label(name)}"])
        path = _path(agents_dir, name)
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed
```

In `pulse/run.py`: `import plistlib` and `from pulse import schedule` at the top; add the parser:

```python
    sched = sub.add_parser("schedule", help="install launchd jobs: pipeline every 30 min, weekly digest, optional bot")
    sched.add_argument("action", choices=("install", "uninstall", "show"))
    sched.add_argument("--with-bot", action="store_true", help="also keep the live bot running")
    sched.add_argument("--env-file", type=Path, default=schedule.DEFAULT_ENV_FILE,
                       help="file of KEY=value lines the jobs load at start (never copied into the plists)")
```

the handler:

```python
def _schedule(args) -> int:
    config = Path(args.config).expanduser().resolve()
    if not config.exists():
        print(f"schedule: {config} not found", file=sys.stderr)
        return 2
    names = ["pipeline", "digest"] + (["bot"] if args.with_bot else [])
    common = dict(python=Path(sys.executable), config=config, env_file=args.env_file.expanduser(),
                  log_dir=config.parent / "logs")
    if args.action == "show":
        for name in names:
            print(plistlib.dumps(schedule.build_plist(schedule.JOBS[name], **common)).decode())
        return 0
    if args.action == "uninstall":
        removed = schedule.uninstall(["pipeline", "digest", "bot"], agents_dir=schedule.AGENTS_DIR,
                                     runner=schedule.run_launchctl)
        print("removed: " + (", ".join(str(p) for p in removed) or "nothing was installed"))
        return 0
    if not common["env_file"].exists():
        print(f"schedule: warning: {common['env_file']} not found; jobs will start without API keys",
              file=sys.stderr)
    try:
        paths = schedule.install(names, agents_dir=schedule.AGENTS_DIR, runner=schedule.run_launchctl, **common)
    except schedule.ScheduleError as e:
        print(f"schedule: {e}", file=sys.stderr)
        return 1
    for p in paths:
        print(f"installed {p}")
    print(f"logs: {common['log_dir']}")
    return 0
```

and in `main`, right after the `bot-invite` branch: `if args.command == "schedule": return _schedule(args)`.

Add to `README.md` after "Live bot":

````markdown
## Scheduled runs (macOS)

```bash
.venv/bin/python -m pulse.run schedule show                 # print the launchd jobs without installing
.venv/bin/python -m pulse.run schedule install [--with-bot] # pipeline every 30 min, digest Mondays 09:00
.venv/bin/python -m pulse.run schedule uninstall
```

launchd does not see your shell's environment, so each job loads `~/.config/discord-pulse/env` (change it with `--env-file`) before it runs. Put `export NAME=value` lines for the keys you use in that file and keep it `chmod 600`; the keys are never copied into the plists. Logs go to `logs/` next to `pulse.toml`.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_schedule.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/schedule.py pulse/run.py README.md tests/test_schedule.py
git commit -m "feat: launchd schedules for the pipeline, weekly digest and bot

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: Send a pain point to GitHub or Linear

**Files:**
- Create: `pulse/issues.py`
- Modify: `pulse/config.py` (`IntegrationsConfig`, `Config.integrations`), `pulse/db.py` (`theme_issues` table), `pulse/run.py` (`issue` command), `pulse.toml.example`, `README.md`
- Test: `tests/test_issues.py`, `tests/test_config.py` (append)

**Interfaces:**
- Consumes: `pulse.stats.theme_resolution`, `theme_scores(conn, now, *, limit)` (`ThemeScore.theme_id, volume, prev_volume, mean_negativity`), `sample_messages(conn, start, end, *, theme_id, limit)`; `pulse.theme_status.status_for(conn, theme_id) -> {"status", "label", "note", ...}`; `pulse.links.jump_link(guild_id, channel_id, message_id)`; `pulse.themes.create_theme(conn, name, description, now) -> (id, created)`, `assign(conn, message_id, theme_id)`, `merge_themes(conn, from_id, into_id, now)`.
- Produces: `IntegrationsConfig(github_repo: str | None = None, github_labels: tuple[str, ...] = (), linear_team_id: str | None = None)`; `Config.integrations` (default `IntegrationsConfig()`); table `theme_issues(theme_id, tracker, url, identifier, created_at, PRIMARY KEY (theme_id, tracker))`; in `pulse/issues.py`: `TRACKERS = ("github", "linear")`, `TRACKER_ENV`, `TRACKER_LABELS`, `TrackerError`, `target(config, tracker) -> str | None`, `issue_draft(conn, theme_id, now) -> {"theme_id", "title", "body"}` (raises `LookupError`), `existing_issue(conn, theme_id, tracker) -> {"url", "identifier"} | None`, `tracker_status(conn, config, theme_id, env=None) -> list[dict]` (one entry per configured tracker: `tracker, label, target, existing, ready, reason`), `send_issue(conn, config, theme_id, tracker, now, *, client=None, env=None) -> {"url", "identifier", "created": bool}`.

The issue body has the theme's description, counts for the last 7 days against the 7 before, average negativity, status and note, and up to 8 message excerpts from the last 30 days (whitespace collapsed, 200 characters, `@` defused so no one on GitHub is pinged) with Discord links. It names no authors. Everything is keyed on the root theme, so a merged theme maps to the issue already sent for its target.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_integrations_section(tmp_path):
    from pulse.config import ConfigError, IntegrationsConfig, load_config

    base = (
        '[server]\nguild_id = "1"\n[models]\ntriage = "anthropic:m"\ntheme = "anthropic:m"\n'
        'digest = "anthropic:m"\ninvestigate = "anthropic:m"\n[pricing."anthropic:m"]\ninput = 1\noutput = 2\n'
    )
    path = tmp_path / "pulse.toml"
    path.write_text(base)
    assert load_config(path, {}, require_keys=False).integrations == IntegrationsConfig()
    path.write_text(base + '[integrations.github]\nrepo = "acme/sdk"\nlabels = ["community"]\n'
                           '[integrations.linear]\nteam_id = "abc-123"\n')
    cfg = load_config(path, {}, require_keys=False).integrations
    assert (cfg.github_repo, cfg.github_labels, cfg.linear_team_id) == ("acme/sdk", ("community",), "abc-123")
    path.write_text(base + '[integrations.github]\nrepo = "not a repo"\n')
    with pytest.raises(ConfigError, match="owner/name"):
        load_config(path, {}, require_keys=False)
```

`tests/test_issues.py`:

```python
import json
from datetime import timedelta

import httpx
import pytest

from pulse.config import IntegrationsConfig
from pulse.db import connect
from pulse.issues import TrackerError, issue_draft, send_issue, tracker_status
from pulse.links import jump_link
from pulse.store import upsert_messages
from pulse.themes import assign, create_theme, merge_themes
from tests.fakes import T0, make_config, msg, set_triage

NOW = T0 + timedelta(days=1)
GH = IntegrationsConfig(github_repo="acme/sdk", github_labels=("community",), linear_team_id="team-1")


def seed():
    conn = connect(":memory:")
    upsert_messages(conn, [
        msg("m1", "install fails on M1 @everyone", minutes=0, author_name="alice"),
        msg("m2", "same here\n\nstill broken", minutes=10, author_id="u2", author_name="bob"),
    ], frozenset({"t1"}))
    set_triage(conn, "m1", sentiment=-2, kind="bug", topics=("install",))
    set_triage(conn, "m2", sentiment=-1, kind="bug", topics=("install",))
    with conn:
        a, _ = create_theme(conn, "M1 install", "Wheels missing for arm64", T0)
        b, _ = create_theme(conn, "Apple silicon", "", T0)
        assign(conn, "m1", a)
        assign(conn, "m2", b)
        merge_themes(conn, b, a, T0)
    return conn, a, b


def client(handler, seen):
    def wrapped(request):
        seen.append(request)
        return handler(request)
    return httpx.Client(transport=httpx.MockTransport(wrapped))


def github_ok(request):
    return httpx.Response(201, json={"html_url": "https://github.com/acme/sdk/issues/12", "number": 12})


def test_issue_draft_summarises_without_author_names():
    conn, a, b = seed()
    d = issue_draft(conn, b, NOW)
    assert d["theme_id"] == a and d["title"] == "Community pain point: M1 install"
    body = d["body"]
    assert "Wheels missing for arm64" in body
    assert "Messages in the last 7 days: 2 (previous 7 days: 0)" in body
    assert "Average negativity: 1.5 of 2" in body and "Status: Not triaged" in body
    assert "@​everyone" in body and "same here still broken" in body
    assert jump_link("900", "100", "m1") in body
    assert "alice" not in body and "bob" not in body
    with pytest.raises(LookupError):
        issue_draft(conn, 999, NOW)


def test_send_github_issue_and_store_it():
    conn, a, _ = seed()
    seen = []
    result = send_issue(conn, make_config(integrations=GH), a, "github", NOW,
                        client=client(github_ok, seen), env={"GITHUB_TOKEN": "ghp"})
    assert result == {"url": "https://github.com/acme/sdk/issues/12", "identifier": "#12", "created": True}
    [req] = seen
    assert str(req.url) == "https://api.github.com/repos/acme/sdk/issues"
    assert req.headers["Authorization"] == "Bearer ghp"
    payload = json.loads(req.content)
    assert payload["title"] == "Community pain point: M1 install" and payload["labels"] == ["community"]


def test_send_issue_is_idempotent_and_uses_root_theme():
    conn, a, b = seed()
    seen = []
    c = client(github_ok, seen)
    send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=c, env={"GITHUB_TOKEN": "x"})
    again = send_issue(conn, make_config(integrations=GH), b, "github", NOW, client=c, env={"GITHUB_TOKEN": "x"})
    assert again == {"url": "https://github.com/acme/sdk/issues/12", "identifier": "#12", "created": False}
    assert len(seen) == 1


def test_send_linear_issue():
    conn, a, _ = seed()
    seen = []

    def linear_ok(request):
        return httpx.Response(200, json={"data": {"issueCreate": {"success": True, "issue": {
            "identifier": "ENG-7", "url": "https://linear.app/acme/issue/ENG-7"}}}})

    result = send_issue(conn, make_config(integrations=GH), a, "linear", NOW,
                        client=client(linear_ok, seen), env={"LINEAR_API_KEY": "lin_api"})
    assert result["identifier"] == "ENG-7" and result["created"]
    [req] = seen
    assert str(req.url) == "https://api.linear.app/graphql" and req.headers["Authorization"] == "lin_api"
    assert json.loads(req.content)["variables"]["input"]["teamId"] == "team-1"


@pytest.mark.parametrize("handler, message", [
    (lambda r: httpx.Response(404, json={"message": "Not Found"}), "GitHub returned 404: Not Found"),
    (lambda r: (_ for _ in ()).throw(httpx.ConnectError("refused")), "could not reach GitHub"),
])
def test_github_failures_store_nothing(handler, message):
    conn, a, _ = seed()
    with pytest.raises(TrackerError, match=message):
        send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=client(handler, []),
                   env={"GITHUB_TOKEN": "x"})
    assert conn.execute("SELECT COUNT(*) FROM theme_issues").fetchone()[0] == 0


def test_linear_graphql_errors_raise():
    conn, a, _ = seed()
    bad = lambda r: httpx.Response(200, json={"errors": [{"message": "team not found"}]})
    with pytest.raises(TrackerError, match="team not found"):
        send_issue(conn, make_config(integrations=GH), a, "linear", NOW, client=client(bad, []),
                   env={"LINEAR_API_KEY": "x"})


def test_missing_token_or_setup_fails_before_any_request():
    conn, a, _ = seed()
    seen = []
    with pytest.raises(TrackerError, match="GITHUB_TOKEN must be set"):
        send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=client(github_ok, seen), env={})
    with pytest.raises(TrackerError, match=r"\[integrations.github\]"):
        send_issue(conn, make_config(), a, "github", NOW, client=client(github_ok, seen), env={"GITHUB_TOKEN": "x"})
    with pytest.raises(ValueError):
        send_issue(conn, make_config(integrations=GH), a, "jira", NOW, env={})
    assert seen == []


def test_tracker_status_reports_targets_tokens_and_existing_issues():
    conn, a, _ = seed()
    status = tracker_status(conn, make_config(integrations=GH), a, env={"GITHUB_TOKEN": "x"})
    assert [(s["tracker"], s["target"], s["ready"], s["existing"]) for s in status] == [
        ("github", "acme/sdk", True, None), ("linear", "team-1", False, None)]
    assert status[1]["reason"] == "Set LINEAR_API_KEY to send issues to Linear"
    send_issue(conn, make_config(integrations=GH), a, "github", NOW, client=client(github_ok, []),
               env={"GITHUB_TOKEN": "x"})
    assert tracker_status(conn, make_config(integrations=GH), a, env={})[0]["existing"]["identifier"] == "#12"
    assert tracker_status(conn, make_config(), a, env={}) == []


def test_issue_cli_dry_run_prints_the_draft(tmp_path, monkeypatch, capsys):
    from pulse.run import main
    from tests.test_cli import CONFIG

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    (tmp_path / "pulse.toml").write_text(CONFIG + '\n[integrations.github]\nrepo = "acme/sdk"\n')
    conn = connect(tmp_path / "pulse.db")
    upsert_messages(conn, [msg("m1", "install fails")], frozenset())
    set_triage(conn, "m1", sentiment=-2, kind="bug")
    with conn:
        tid, _ = create_theme(conn, "M1 install", "", T0)
        assign(conn, "m1", tid)
    conn.close()
    cfg = str(tmp_path / "pulse.toml")
    assert main(["--config", cfg, "issue", str(tid), "--to", "github", "--dry-run"]) == 0
    assert "Community pain point: M1 install" in capsys.readouterr().out
    assert main(["--config", cfg, "issue", str(tid), "--to", "github"]) == 1
    assert "GITHUB_TOKEN must be set" in capsys.readouterr().err
    assert main(["--config", cfg, "issue", "999", "--to", "github", "--dry-run"]) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_issues.py tests/test_config.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.issues'`

- [ ] **Step 3: Write the implementation**

In `pulse/config.py` (add `import re`):

```python
@dataclass(frozen=True)
class IntegrationsConfig:
    github_repo: str | None = None
    github_labels: tuple[str, ...] = ()
    linear_team_id: str | None = None


def _integrations(raw: Any) -> IntegrationsConfig:
    raw = raw or {}
    github, linear = raw.get("github") or {}, raw.get("linear") or {}
    repo = github.get("repo")
    if repo is not None and not re.fullmatch(r"[\w.-]+/[\w.-]+", str(repo)):
        raise ConfigError(f"[integrations.github] repo must look like owner/name, got {repo!r}")
    team = linear.get("team_id")
    return IntegrationsConfig(
        github_repo=str(repo) if repo else None,
        github_labels=tuple(str(x) for x in github.get("labels", [])),
        linear_team_id=str(team) if team else None,
    )
```

Add `integrations: IntegrationsConfig = IntegrationsConfig()` as the last `Config` field and pass `integrations=_integrations(raw.get("integrations"))` in `load_config`. (`IntegrationsConfig` must be defined above `Config`.)

In `pulse/db.py`, add to `SCHEMA` after the `theme_status` table:

```sql
CREATE TABLE IF NOT EXISTS theme_issues (
    theme_id INTEGER NOT NULL REFERENCES themes(id),
    tracker TEXT NOT NULL CHECK (tracker IN ('github', 'linear')),
    url TEXT NOT NULL,
    identifier TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (theme_id, tracker)
);
```

`pulse/issues.py`:

```python
"""Send a pain point to GitHub or Linear as an issue with its evidence links.

Tokens come from the environment when an issue is sent. Each (root theme, tracker)
gets at most one issue; asking again returns the stored link.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Mapping

import httpx

from pulse.config import Config
from pulse.links import jump_link
from pulse.models import to_iso
from pulse.stats import sample_messages, theme_resolution, theme_scores
from pulse.theme_status import status_for

TRACKERS = ("github", "linear")
TRACKER_ENV = {"github": "GITHUB_TOKEN", "linear": "LINEAR_API_KEY"}
TRACKER_LABELS = {"github": "GitHub", "linear": "Linear"}
EVIDENCE_LIMIT = 8
EVIDENCE_DAYS = 30
EXCERPT_CHARS = 200
TIMEOUT = 20.0
_LINEAR_MUTATION = (
    "mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { identifier url } } }"
)


class TrackerError(Exception):
    pass


def target(config: Config, tracker: str) -> str | None:
    return config.integrations.github_repo if tracker == "github" else config.integrations.linear_team_id


def _root(conn: sqlite3.Connection, theme_id: int) -> int:
    root = theme_resolution(conn).get(theme_id)
    if root is None:
        raise LookupError(f"unknown theme {theme_id}")
    return root


def _excerpt(text: str) -> str:
    one = " ".join(text.split()).replace("@", "@​")  # no one on the tracker gets pinged
    return one if len(one) <= EXCERPT_CHARS else one[: EXCERPT_CHARS - 1] + "…"


def issue_draft(conn: sqlite3.Connection, theme_id: int, now: datetime) -> dict:
    root = _root(conn, theme_id)
    theme = conn.execute("SELECT name, description FROM themes WHERE id = ?", (root,)).fetchone()
    score = next((s for s in theme_scores(conn, now, limit=1000) if s.theme_id == root), None)
    status = status_for(conn, root)
    sample = sample_messages(conn, now - timedelta(days=EVIDENCE_DAYS), now, theme_id=root, limit=EVIDENCE_LIMIT)
    ids = [m["message_id"] for m in sample]
    where = {}
    if ids:
        where = {r["id"]: r for r in conn.execute(
            f"SELECT id, guild_id, channel_id FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)}
    lines = ["Reported by the community on Discord. Summary generated by Discord Pulse.", ""]
    if theme["description"]:
        lines += [theme["description"], ""]
    if score is not None:
        lines += [
            f"- Messages in the last 7 days: {score.volume} (previous 7 days: {score.prev_volume})",
            f"- Average negativity: {score.mean_negativity:.1f} of 2",
        ]
    lines.append(f"- Status: {status['label']}" + (f" ({status['note']})" if status["note"] else ""))
    if sample:
        lines += ["", "### Example messages", ""]
        for m in sample:
            r = where[m["message_id"]]
            link = jump_link(r["guild_id"], r["channel_id"], m["message_id"])
            lines.append(f"- {_excerpt(m['content'])} ([open in Discord]({link}))")
    return {"theme_id": root, "title": f"Community pain point: {theme['name']}", "body": "\n".join(lines)}


def existing_issue(conn: sqlite3.Connection, theme_id: int, tracker: str) -> dict | None:
    row = conn.execute(
        "SELECT url, identifier FROM theme_issues WHERE theme_id = ? AND tracker = ?",
        (_root(conn, theme_id), tracker),
    ).fetchone()
    return {"url": row["url"], "identifier": row["identifier"]} if row else None


def tracker_status(conn, config: Config, theme_id: int, env: Mapping[str, str] | None = None) -> list[dict]:
    env = os.environ if env is None else env
    out = []
    for tracker in TRACKERS:
        tgt = target(config, tracker)
        if not tgt:
            continue
        ready = bool(env.get(TRACKER_ENV[tracker]))
        out.append({
            "tracker": tracker, "label": TRACKER_LABELS[tracker], "target": tgt,
            "existing": existing_issue(conn, theme_id, tracker), "ready": ready,
            "reason": None if ready else f"Set {TRACKER_ENV[tracker]} to send issues to {TRACKER_LABELS[tracker]}",
        })
    return out


def _post(client: httpx.Client, name: str, url: str, **kwargs) -> httpx.Response:
    try:
        return client.post(url, timeout=TIMEOUT, **kwargs)
    except httpx.HTTPError as e:
        raise TrackerError(f"could not reach {name}: {e}") from e


def create_github_issue(client, repo, token, title, body, labels) -> tuple[str, str]:
    r = _post(client, "GitHub", f"https://api.github.com/repos/{repo}/issues",
              headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                       "X-GitHub-Api-Version": "2022-11-28"},
              json={"title": title, "body": body, "labels": list(labels)})
    if r.status_code >= 400:
        try:
            detail = r.json().get("message", r.text[:200])
        except ValueError:
            detail = r.text[:200]
        raise TrackerError(f"GitHub returned {r.status_code}: {detail}")
    data = r.json()
    return data["html_url"], f"#{data['number']}"


def create_linear_issue(client, team_id, token, title, body) -> tuple[str, str]:
    r = _post(client, "Linear", "https://api.linear.app/graphql",
              headers={"Authorization": token, "Content-Type": "application/json"},
              json={"query": _LINEAR_MUTATION,
                    "variables": {"input": {"teamId": team_id, "title": title, "description": body}}})
    if r.status_code >= 400:
        raise TrackerError(f"Linear returned {r.status_code}: {r.text[:200]}")
    data = r.json()
    if data.get("errors"):
        raise TrackerError("Linear: " + "; ".join(str(e.get("message")) for e in data["errors"]))
    created = (data.get("data") or {}).get("issueCreate") or {}
    if not created.get("success"):
        raise TrackerError("Linear did not create the issue")
    return created["issue"]["url"], created["issue"]["identifier"]


def send_issue(conn, config: Config, theme_id: int, tracker: str, now: datetime, *,
               client: httpx.Client | None = None, env: Mapping[str, str] | None = None) -> dict:
    if tracker not in TRACKERS:
        raise ValueError(f"unknown tracker {tracker!r}; use one of {list(TRACKERS)}")
    tgt = target(config, tracker)
    if not tgt:
        raise TrackerError(f"{TRACKER_LABELS[tracker]} is not set up: add [integrations.{tracker}] to pulse.toml")
    draft = issue_draft(conn, theme_id, now)
    existing = existing_issue(conn, draft["theme_id"], tracker)
    if existing is not None:
        return {**existing, "created": False}
    env = os.environ if env is None else env
    token = env.get(TRACKER_ENV[tracker])
    if not token:
        raise TrackerError(f"{TRACKER_ENV[tracker]} must be set to send issues to {TRACKER_LABELS[tracker]}")
    own = client is None
    client = client or httpx.Client()
    try:
        if tracker == "github":
            url, ident = create_github_issue(client, tgt, token, draft["title"], draft["body"],
                                             config.integrations.github_labels)
        else:
            url, ident = create_linear_issue(client, tgt, token, draft["title"], draft["body"])
    finally:
        if own:
            client.close()
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO theme_issues (theme_id, tracker, url, identifier, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (draft["theme_id"], tracker, url, ident, to_iso(now)),
        )
    return {"url": url, "identifier": ident, "created": True}
```

In `pulse/run.py`, import `from pulse.issues import TRACKER_LABELS, TrackerError, issue_draft, send_issue`, add the parser:

```python
    issue = sub.add_parser("issue", help="send a pain point to GitHub or Linear as an issue")
    issue.add_argument("theme", type=int, help="theme id (shown in the dashboard's Pain points link)")
    issue.add_argument("--to", choices=("github", "linear"), required=True)
    issue.add_argument("--dry-run", action="store_true", help="print the issue instead of sending it")
```

and the branch (after `investigate`):

```python
    elif args.command == "issue":
        try:
            if args.dry_run:
                draft = issue_draft(conn, args.theme, now)
                print(draft["title"] + "\n\n" + draft["body"])
                return 0
            result = send_issue(conn, config, args.theme, args.to, now)
        except (LookupError, TrackerError) as e:
            print(f"issue: {e.args[0]}", file=sys.stderr)
            return 1
        verb = "created" if result["created"] else "already sent:"
        print(f"{verb} {TRACKER_LABELS[args.to]} issue {result['identifier']}: {result['url']}")
```

Append to `pulse.toml.example`:

```toml
# Send a pain point to an issue tracker from the dashboard or `pulse.run issue`.
# Tokens come from GITHUB_TOKEN / LINEAR_API_KEY, never from this file.
# [integrations.github]
# repo = "your-org/your-repo"
# labels = ["community"]
# [integrations.linear]
# team_id = "your-team-uuid"
```

Add to `README.md` after "Scheduled runs":

````markdown
## Send a pain point to GitHub or Linear

Add `[integrations.github]` (`repo`, optional `labels`) or `[integrations.linear]` (`team_id`) to `pulse.toml` and set `GITHUB_TOKEN` (a fine-grained token with Issues: write on that repo) or `LINEAR_API_KEY`. Then use the button on a pain point, or:

```bash
.venv/bin/python -m pulse.run issue 7 --to github --dry-run   # see exactly what would be posted
.venv/bin/python -m pulse.run issue 7 --to github
```

The issue holds the pain point's summary, counts, status and up to 8 message excerpts with Discord links. It names no authors, and `@` mentions are defused. Each pain point is sent at most once per tracker; asking again returns the existing link. Check `--dry-run` before posting to a public repo.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_issues.py tests/test_config.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/issues.py pulse/config.py pulse/db.py pulse/run.py pulse.toml.example README.md tests/test_issues.py tests/test_config.py
git commit -m "feat: send a pain point to GitHub or Linear with its evidence links

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 7: "Send to GitHub / Linear" on the Pain points page

**Files:**
- Modify: `pulse/web/settings.py` (`env`, `http_client_factory`), `pulse/web/views/pain.py`, `pulse/web/templates/pain.html`, `tests/web_fakes.py` (`make_client` passes `env` and `http_client_factory`)
- Test: `tests/test_web_issues.py`

**Interfaces:**
- Consumes: Task 6 (`tracker_status`, `send_issue`, `TrackerError`, `TRACKER_LABELS`); `pulse.stats.theme_resolution`; `Filters.qs(**extra)`; the existing cross-site middleware (all POSTs are already refused when cross-site).
- Produces: `WebSettings.env: Mapping[str, str] | None = None` (None means `os.environ`, read at request time), `WebSettings.http_client_factory: Callable[[], httpx.Client] | None = None`; `POST /pain/{theme_id}/issue` (form field `tracker`): 303 to `/pain?...&theme=<root>&issued=created|exists`; 400 unknown tracker, 404 unknown theme, 502 tracker failure (detail is the tracker's message). `make_client(..., env=None, http_client_factory=None)`.

The page shows one control per configured tracker: a link to the existing issue, a "Send to GitHub" button whose tooltip says exactly what is posted, or a disabled button saying which token to set. Nothing shows when no tracker is configured (including demo mode).

- [ ] **Step 1: Write the failing tests**

In `tests/web_fakes.py`, change `make_client` to accept and pass the two new settings:

```python
def make_client(
    tmp_path: Path, *, seeded=True, llm_factory=None, demo=False, config=CONFIG, allowed_hosts=None,
    env=None, http_client_factory=None, **client_kw
) -> TestClient:
    ...
    settings = WebSettings(
        db_path=path, config=config, server_name="Acme SDK Community", demo=demo,
        clock=lambda: NOW, llm_factory=llm_factory, allowed_hosts=allowed_hosts,
        env=env, http_client_factory=http_client_factory,
    )
```

`tests/test_web_issues.py`:

```python
from dataclasses import replace

import httpx

from pulse.config import IntegrationsConfig
from tests.web_fakes import CONFIG, make_client

GH = replace(CONFIG, integrations=IntegrationsConfig(github_repo="acme/sdk", linear_team_id="team-1"))


def github(status=201, body=None):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json=body or {"html_url": "https://github.com/acme/sdk/issues/12", "number": 12})

    return calls, (lambda: httpx.Client(transport=httpx.MockTransport(handler)))


def test_no_tracker_controls_without_integrations(tmp_path):
    assert "Send to" not in make_client(tmp_path).get("/pain").text


def test_controls_show_ready_and_disabled_trackers(tmp_path):
    html = make_client(tmp_path, config=GH, env={"GITHUB_TOKEN": "x"}).get("/pain?theme=1").text
    assert "Send to GitHub" in html and "no author names" in html and "acme/sdk" in html
    assert "Set LINEAR_API_KEY to send issues to Linear" in html


def test_send_creates_once_then_links(tmp_path):
    calls, factory = github()
    client = make_client(tmp_path, config=GH, env={"GITHUB_TOKEN": "x"}, http_client_factory=factory)
    r = client.post("/pain/1/issue", data={"tracker": "github"}, follow_redirects=False)
    assert r.status_code == 303 and "theme=1" in r.headers["location"] and "issued=created" in r.headers["location"]
    html = client.get(r.headers["location"]).text
    assert 'href="https://github.com/acme/sdk/issues/12"' in html and "GitHub issue #12" in html
    assert "Issue created." in html
    r2 = client.post("/pain/1/issue", data={"tracker": "github"}, follow_redirects=False)
    assert "issued=exists" in r2.headers["location"] and len(calls) == 1


def test_send_errors(tmp_path):
    _, factory = github(404, {"message": "Not Found"})
    client = make_client(tmp_path, config=GH, env={"GITHUB_TOKEN": "x"}, http_client_factory=factory)
    r = client.post("/pain/1/issue", data={"tracker": "github"})
    assert r.status_code == 502 and "GitHub returned 404: Not Found" in r.text
    assert client.post("/pain/1/issue", data={"tracker": "jira"}).status_code == 400
    assert client.post("/pain/999/issue", data={"tracker": "github"}).status_code == 404
    cross = client.post("/pain/1/issue", data={"tracker": "github"}, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_web_issues.py -q`
Expected: FAIL (`TypeError: WebSettings.__init__() got an unexpected keyword argument 'env'`)

- [ ] **Step 3: Write the implementation**

In `pulse/web/settings.py`, add `import httpx` and `from typing import Mapping`, and two fields after `allowed_hosts`:

```python
    env: Mapping[str, str] | None = None
    http_client_factory: Callable[[], httpx.Client] | None = None
```

In `pulse/web/views/pain.py`, add imports `from pulse.issues import TrackerError, send_issue, tracker_status`; in `pain()` add the parameter `issued: str | None = None`, compute

```python
    settings = request.app.state.settings
    trackers = tracker_status(conn, settings.config, sel["id"], env=settings.env) if sel is not None else []
```

and pass `trackers=trackers, issued={"created": "Issue created.", "exists": "Already sent; here is the issue."}.get(issued)` to `render`. Add the route:

```python
@router.post("/pain/{theme_id}/issue")
def send_to_tracker(
    request: Request,
    theme_id: int,
    tracker: str = Form(...),
    conn=Depends(get_conn),
    f: Filters = Depends(get_filters),
):
    settings = request.app.state.settings
    client = settings.http_client_factory() if settings.http_client_factory else None
    try:
        result = send_issue(conn, settings.config, theme_id, tracker, f.now, client=client, env=settings.env)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except TrackerError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    finally:
        if client is not None:
            client.close()
    root = stats.theme_resolution(conn)[theme_id]
    issued = "created" if result["created"] else "exists"
    return RedirectResponse(f"/pain{f.qs(theme=root, issued=issued)}", status_code=303)
```

In `pulse/web/templates/pain.html`, insert right after the status form's closing `</form>`:

```html
    {% if trackers %}
    <div class="status-row" aria-label="Issue trackers">
      {% for t in trackers %}
        {% if t.existing and t.existing.url.startswith('https://') %}
          <a class="btn" href="{{ t.existing.url }}" target="_blank" rel="noopener">{{ t.label }} issue {{ t.existing.identifier }}</a>
        {% elif t.ready %}
          <form method="post" action="/pain/{{ sel.id }}/issue{{ f.qs() }}">
            <input type="hidden" name="tracker" value="{{ t.tracker }}">
            <button class="btn" type="submit" title="Posts the summary, counts and up to 8 message excerpts with Discord links (no author names) to {{ t.target }}">Send to {{ t.label }}</button>
          </form>
        {% else %}
          <button class="btn" type="button" disabled title="{{ t.reason }}">Send to {{ t.label }}</button>
          <span class="pmeta">{{ t.reason }}</span>
        {% endif %}
      {% endfor %}
      {% if issued %}<span class="pmeta" role="status">{{ issued }}</span>{% endif %}
    </div>
    {% endif %}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_web_issues.py tests/test_web_pain.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/web/settings.py pulse/web/views/pain.py pulse/web/templates/pain.html tests/web_fakes.py tests/test_web_issues.py
git commit -m "feat: send a pain point to GitHub or Linear from the dashboard

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 8: Slack alerts for spiking pain points and frustrated users left unanswered

**Files:**
- Create: `pulse/alerts.py`
- Modify: `pulse/config.py` (`AlertsConfig`, `Config.alerts`), `pulse/db.py` (`alerts_sent` table), `pulse/pipeline.py` (alerts after themes; `PipelineReport.alerts`; `format_alerts`), `pulse/run.py` (`alerts [--dry-run]`), `pulse.toml.example`, `README.md`
- Test: `tests/test_alerts.py`

**Interfaces:**
- Consumes: `pulse.stats.theme_scores(conn, now, *, window_days, limit)`, `sample_messages`, `first_team_reply(conn, message_id, thread_id, created_at, *, before=None)`; `pulse.links.jump_link`; `pulse.models.to_iso`, `from_iso`.
- Produces: `AlertsConfig(enabled=False, spike_min_volume=5, spike_trend=1.0, frustrated_hours=12.0)`, `Config.alerts` (default `AlertsConfig()`); table `alerts_sent(kind, key, sent_at, PRIMARY KEY (kind, key))`; in `pulse/alerts.py`: `SLACK_ENV = "SLACK_WEBHOOK_URL"`, `MAX_PER_RUN = 10`, `Alert(kind, key, text)`, `AlertStats(found=0, sent=0, failed=0, skipped=None)`, `find_alerts(conn, config, now) -> list[Alert]`, `send_alerts(conn, config, now, *, client=None, env=None) -> AlertStats`; `PipelineReport.alerts: AlertStats | None = None`; `format_alerts(stats) -> str`.

Rules:
- **Spike:** a theme with at least `spike_min_volume` community messages in the last 24 hours and trend ≥ `spike_trend` against the 24 hours before (1.0 means it doubled). The key is `<theme id>:<UTC date>`, so a theme alerts at most once a day.
- **Frustrated and unanswered:** an open `frustrated` mod queue item whose message is at least `frustrated_hours` old and has no staff reply yet. The key is the queue item id, so each item alerts once.

User text is escaped for Slack (`&`, `<`, `>`). An alert is recorded as sent only after Slack accepts it; on the first failure the run stops and the rest retry next time. At most 10 alerts go out per run. The webhook URL is never printed.

- [ ] **Step 1: Write the failing tests**

`tests/test_alerts.py`:

```python
import json
from datetime import timedelta

import httpx
import pytest

from pulse.alerts import MAX_PER_RUN, find_alerts, send_alerts
from pulse.config import AlertsConfig
from pulse.db import connect
from pulse.models import to_iso
from pulse.pipeline import format_alerts
from pulse.store import upsert_messages
from pulse.themes import assign, create_theme
from tests.fakes import T0, make_config, msg, set_triage

NOW = T0 + timedelta(days=1)
CFG = make_config(alerts=AlertsConfig(enabled=True))


def seed(spike=6, frustrated_age_hours=13, staff_reply=False):
    conn = connect(":memory:")
    base = 24 * 60  # NOW is T0 + 1 day, in minutes from T0
    msgs = [msg(f"s{i}", f"deploy hangs <again> & again {i}", minutes=base - 60 - i, author_id=f"u{i}")
            for i in range(spike)]
    msgs.append(msg("f1", "this is the THIRD outage <b>today</b>", minutes=base - frustrated_age_hours * 60,
                    author_id="u9", author_name="erin"))
    if staff_reply:
        msgs.append(msg("r1", "looking into it", minutes=base - 30, author_id="t1", author_name="sam",
                        reply_to_id="f1"))
    upsert_messages(conn, msgs, frozenset({"t1"}))
    for m in msgs:
        set_triage(conn, m.id, sentiment=-2 if m.id == "f1" else -1, kind="bug", topics=("deploy",))
    with conn:
        tid, _ = create_theme(conn, "WSL deploy hangs", "", T0)
        for m in msgs[:spike]:
            assign(conn, m.id, tid)
        conn.execute(
            "INSERT INTO mod_queue (queue_key, message_id, reason, status, opened_at) VALUES ('f1', 'f1',"
            " 'frustrated', 'open', ?)", (to_iso(NOW),))
    return conn


def slack(status=200):
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(status, text="ok")

    return calls, httpx.Client(transport=httpx.MockTransport(handler))


def test_find_alerts_spike_and_frustrated_with_escaping():
    alerts = find_alerts(seed(), CFG, NOW)
    assert [a.kind for a in alerts] == ["spike", "frustrated"]
    spike, frus = alerts
    assert spike.key == f"1:{NOW.date().isoformat()}"
    assert "*WSL deploy hangs*: 6 messages in the last 24 hours (previous 24 hours: 0)" in spike.text
    assert "&lt;again&gt; &amp; again" in spike.text and "<again>" not in spike.text
    assert "|Open in Discord>" in spike.text
    assert frus.key == "1" and "unanswered for 13 hours: erin in #help" in frus.text
    assert "&lt;b&gt;today&lt;/b&gt;" in frus.text


def test_thresholds_and_staff_replies_suppress_alerts():
    assert find_alerts(seed(spike=4), CFG, NOW)[0].kind == "frustrated"   # below spike_min_volume
    assert [a.kind for a in find_alerts(seed(frustrated_age_hours=6), CFG, NOW)] == ["spike"]
    assert [a.kind for a in find_alerts(seed(staff_reply=True), CFG, NOW)] == ["spike"]


def test_send_alerts_posts_once_and_records():
    conn = seed()
    calls, client = slack()
    stats = send_alerts(conn, CFG, NOW, client=client, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"})
    assert (stats.found, stats.sent, stats.failed) == (2, 2, 0) and len(calls) == 2
    again = send_alerts(conn, CFG, NOW + timedelta(minutes=30), client=client,
                        env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"})
    assert (again.found, again.sent) == (0, 0) and len(calls) == 2
    assert format_alerts(stats) == "alerts: 2 found, 2 sent"


def test_failed_post_is_retried_next_run():
    conn = seed()
    _, bad = slack(500)
    stats = send_alerts(conn, CFG, NOW, client=bad, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"})
    assert (stats.sent, stats.failed) == (0, 1)
    assert format_alerts(stats) == "alerts: 2 found, 0 sent, 1 failed (will retry)"
    calls, good = slack()
    assert send_alerts(conn, CFG, NOW, client=good, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"}).sent == 2


def test_missing_webhook_skips_without_error():
    stats = send_alerts(seed(), CFG, NOW, env={})
    assert stats.found == 2 and stats.sent == 0 and stats.skipped == "SLACK_WEBHOOK_URL is not set"
    assert "https://" not in format_alerts(stats)


def test_at_most_ten_alerts_per_run(monkeypatch):
    from pulse import alerts as mod
    from pulse.alerts import Alert

    monkeypatch.setattr(mod, "find_alerts", lambda c, cfg, now: [Alert("frustrated", str(i), "x") for i in range(15)])
    calls, client = slack()
    assert send_alerts(seed(), CFG, NOW, client=client, env={"SLACK_WEBHOOK_URL": "https://hooks.slack.test/x"}).sent == MAX_PER_RUN == 10


def test_alerts_config_and_cli_dry_run(tmp_path, monkeypatch, capsys):
    from pulse.config import ConfigError, load_config
    from pulse.run import main
    from tests.test_cli import CONFIG

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    path = tmp_path / "pulse.toml"
    path.write_text(CONFIG + "\n[alerts]\nenabled = true\nspike_min_volume = 3\n")
    assert load_config(path).alerts == AlertsConfig(enabled=True, spike_min_volume=3)
    assert main(["--config", str(path), "alerts", "--dry-run"]) == 0
    assert "no alerts" in capsys.readouterr().out
    path.write_text(CONFIG + '\n[alerts]\nenabled = "yes"\n')
    with pytest.raises(ConfigError, match="enabled"):
        load_config(path)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_alerts.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'pulse.alerts'`

- [ ] **Step 3: Write the implementation**

In `pulse/config.py`:

```python
@dataclass(frozen=True)
class AlertsConfig:
    enabled: bool = False
    spike_min_volume: int = 5
    spike_trend: float = 1.0
    frustrated_hours: float = 12.0


def _alerts(raw: Any) -> AlertsConfig:
    raw = raw or {}
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"[alerts] enabled must be a boolean, got {enabled!r}")
    try:
        cfg = AlertsConfig(
            enabled=enabled,
            spike_min_volume=int(raw.get("spike_min_volume", 5)),
            spike_trend=float(raw.get("spike_trend", 1.0)),
            frustrated_hours=float(raw.get("frustrated_hours", 12.0)),
        )
    except (TypeError, ValueError) as e:
        raise ConfigError(f"[alerts] spike_min_volume, spike_trend and frustrated_hours must be numbers: {e}") from e
    if cfg.spike_min_volume < 1 or cfg.spike_trend < 0 or cfg.frustrated_hours <= 0:
        raise ConfigError("[alerts] spike_min_volume must be at least 1 and the other values positive")
    return cfg
```

Add `alerts: AlertsConfig = AlertsConfig()` as the last `Config` field (define `AlertsConfig` above `Config`) and pass `alerts=_alerts(raw.get("alerts"))` in `load_config`.

In `pulse/db.py`, add to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS alerts_sent (
    kind TEXT NOT NULL CHECK (kind IN ('spike', 'frustrated')),
    key TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
```

`pulse/alerts.py`:

```python
"""Slack alerts: pain points spiking, and frustrated users left without a staff reply.

Each alert is sent once (keyed in alerts_sent), and only recorded after Slack accepts it.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

import httpx

from pulse.config import Config
from pulse.links import jump_link
from pulse.models import from_iso, to_iso
from pulse.stats import first_team_reply, sample_messages, theme_scores

SLACK_ENV = "SLACK_WEBHOOK_URL"
MAX_PER_RUN = 10
TIMEOUT = 20.0


@dataclass(frozen=True)
class Alert:
    kind: str
    key: str
    text: str


@dataclass
class AlertStats:
    found: int = 0
    sent: int = 0
    failed: int = 0
    skipped: str | None = None


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _excerpt(text: str, limit: int = 140) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _link(conn: sqlite3.Connection, message_id: str) -> str:
    r = conn.execute("SELECT guild_id, channel_id FROM messages WHERE id = ?", (message_id,)).fetchone()
    return jump_link(r["guild_id"], r["channel_id"], message_id)


def find_alerts(conn: sqlite3.Connection, config: Config, now: datetime) -> list[Alert]:
    cfg = config.alerts
    sent = {(r["kind"], r["key"]) for r in conn.execute("SELECT kind, key FROM alerts_sent")}
    out: list[Alert] = []
    day = now.astimezone(timezone.utc).date().isoformat()
    for s in theme_scores(conn, now, window_days=1, limit=50):
        key = f"{s.theme_id}:{day}"
        if s.volume < cfg.spike_min_volume or s.trend < cfg.spike_trend or ("spike", key) in sent:
            continue
        text = (f":chart_with_upwards_trend: Pain point spiking: *{_esc(s.name)}*: {s.volume} messages in the "
                f"last 24 hours (previous 24 hours: {s.prev_volume}).")
        top = sample_messages(conn, now - timedelta(days=1), now, theme_id=s.theme_id, limit=1)
        if top:
            text += f"\n> {_esc(_excerpt(top[0]['content']))} <{_link(conn, top[0]['message_id'])}|Open in Discord>"
        out.append(Alert("spike", key, text))
    cutoff = to_iso(now - timedelta(hours=cfg.frustrated_hours))
    rows = conn.execute(
        "SELECT q.id, m.id AS message_id, m.thread_id, m.channel_name, m.author_name, m.content, m.created_at"
        " FROM mod_queue q JOIN messages m ON m.id = q.message_id"
        " WHERE q.status = 'open' AND q.reason = 'frustrated' AND m.created_at <= ?"
        " ORDER BY m.created_at, q.id",
        (cutoff,),
    ).fetchall()
    for r in rows:
        key = str(r["id"])
        if ("frustrated", key) in sent:
            continue
        if first_team_reply(conn, r["message_id"], r["thread_id"], r["created_at"], before=to_iso(now)):
            continue
        hours = int((now - from_iso(r["created_at"])).total_seconds() // 3600)
        text = (f":rotating_light: Frustrated and unanswered for {hours} hours: {_esc(r['author_name'])} in "
                f"#{_esc(r['channel_name'])}\n> {_esc(_excerpt(r['content']))} "
                f"<{_link(conn, r['message_id'])}|Open in Discord>")
        out.append(Alert("frustrated", key, text))
    return out


def send_alerts(conn: sqlite3.Connection, config: Config, now: datetime, *,
                client: httpx.Client | None = None, env: Mapping[str, str] | None = None) -> AlertStats:
    stats = AlertStats()
    alerts = find_alerts(conn, config, now)
    stats.found = len(alerts)
    if not alerts:
        return stats
    env = os.environ if env is None else env
    url = env.get(SLACK_ENV)
    if not url:
        stats.skipped = f"{SLACK_ENV} is not set"
        return stats
    own = client is None
    client = client or httpx.Client()
    try:
        for alert in alerts[:MAX_PER_RUN]:
            try:
                ok = client.post(url, json={"text": alert.text}, timeout=TIMEOUT).status_code < 300
            except httpx.HTTPError:
                ok = False
            if not ok:
                stats.failed += 1
                break  # the rest go next run
            with conn:
                conn.execute("INSERT OR IGNORE INTO alerts_sent (kind, key, sent_at) VALUES (?, ?, ?)",
                             (alert.kind, alert.key, to_iso(now)))
            stats.sent += 1
    finally:
        if own:
            client.close()
    return stats
```

In `pulse/pipeline.py`: import `from pulse.alerts import AlertStats, send_alerts`; add `alerts: AlertStats | None = None` as the last `PipelineReport` field; in `run_pipeline` after `theme_stats = ...`:

```python
    alert_stats = send_alerts(conn, config, now) if config.alerts.enabled else None
    return PipelineReport(ingest_stats, errors, triage_stats, queue_stats, themes=theme_stats, alerts=alert_stats)
```

and add:

```python
def format_alerts(stats: AlertStats) -> str:
    line = f"alerts: {stats.found} found, {stats.sent} sent"
    if stats.failed:
        line += f", {stats.failed} failed (will retry)"
    if stats.skipped:
        line += f" (not sent: {stats.skipped})"
    return line
```

and in `format_report`, after the themes line: `if report.alerts is not None: lines.append(format_alerts(report.alerts))`.

In `pulse/run.py`: import `from pulse.alerts import find_alerts, send_alerts` and `format_alerts`; add the parser:

```python
    alerts = sub.add_parser("alerts", help="post Slack alerts for spiking pain points and frustrated users")
    alerts.add_argument("--dry-run", action="store_true", help="print the alerts instead of posting them")
```

and the branch:

```python
    elif args.command == "alerts":
        if args.dry_run:
            found = find_alerts(conn, config, now)
            print("\n\n".join(a.text for a in found) if found else "no alerts")
        else:
            print(format_alerts(send_alerts(conn, config, now)))
```

Append to `pulse.toml.example`:

```toml
# Slack alerts, posted at the end of each pipeline run. The webhook URL comes from
# SLACK_WEBHOOK_URL, never from this file.
[alerts]
enabled = false
spike_min_volume = 5      # messages in the last 24 hours before a pain point can spike
spike_trend = 1.0         # 1.0 = doubled compared with the 24 hours before
frustrated_hours = 12     # frustrated message with no staff reply for this long
```

Add to `README.md` after the GitHub/Linear section:

````markdown
## Slack alerts

Create a Slack incoming webhook, set `SLACK_WEBHOOK_URL`, and set `[alerts] enabled = true`. Each `pipeline` run then posts:

- a pain point that is spiking (at least `spike_min_volume` messages in 24 hours and `spike_trend` times more than the 24 hours before), at most once a day per pain point;
- a frustrated message that has gone `frustrated_hours` without a staff reply, once per mod queue item.

At most 10 alerts go out per run; a failed post is retried next run. `python -m pulse.run alerts --dry-run` shows what would be posted.
````

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_alerts.py tests/test_pipeline.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/alerts.py pulse/config.py pulse/db.py pulse/pipeline.py pulse/run.py pulse.toml.example README.md tests/test_alerts.py
git commit -m "feat: Slack alerts for spiking pain points and frustrated users left unanswered

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 9: Dashboard follow-ups: start without API keys, any loopback host, no protocol-relative links

**Files:**
- Modify: `pulse/run.py`, `pulse/web/settings.py` (`agents_off_reason`), `pulse/citations.py` (`safe_href`), `README.md`
- Test: `tests/test_cli.py`, `tests/test_citations.py` (append)

**Interfaces:**
- Consumes: Task 2 (`load_config(..., require_keys=)`, `missing_keys`); `pulse.run._serve`, `LOCAL_HOSTS`, `_is_loopback`.
- Produces: `MODEL_COMMANDS = ("triage", "pipeline", "themes", "digest", "investigate")`: only these require provider keys at load time. `WebSettings.agents_off_reason: str | None = None`, used by `agents_off_text` outside demo mode. `_serve` trusts `LOCAL_HOSTS` plus the bound loopback host (and its `[...]` form for IPv6). `safe_href` rejects `//host` and `/\host`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def _capture_app(monkeypatch):
    captured = {}
    monkeypatch.setattr("pulse.run.uvicorn.run", lambda app, **kw: captured.update(app=app, **kw))
    return captured


def test_web_starts_without_keys_with_agents_off(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "pulse.toml").write_text(CONFIG)
    captured = _capture_app(monkeypatch)
    assert main(["--config", str(tmp_path / "pulse.toml"), "web"]) == 0
    settings = captured["app"].state.settings
    assert not settings.agents_on
    assert settings.agents_off_text == "Agents are off: set ANTHROPIC_API_KEY"


def test_model_commands_still_require_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "pulse.toml").write_text(CONFIG)
    assert main(["--config", str(tmp_path / "pulse.toml"), "triage"]) == 2
    assert "ANTHROPIC_API_KEY must be set" in capsys.readouterr().err
    assert main(["--config", str(tmp_path / "pulse.toml"), "modqueue"]) == 0


def test_web_trusts_the_bound_loopback_address(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    (tmp_path / "pulse.toml").write_text(CONFIG)
    captured = _capture_app(monkeypatch)
    assert main(["--config", str(tmp_path / "pulse.toml"), "web", "--host", "127.0.0.5"]) == 0
    assert "127.0.0.5" in captured["app"].state.settings.allowed_hosts
    assert main(["--config", str(tmp_path / "pulse.toml"), "web", "--host", "::1"]) == 0
    assert "[::1]" in captured["app"].state.settings.allowed_hosts
```

Append to `tests/test_citations.py`:

```python
def test_protocol_relative_links_are_dropped():
    from pulse.citations import safe_href

    assert not safe_href("//evil.com/x") and not safe_href("/\\evil.com") and not safe_href(" //evil.com")
    assert safe_href("/pain?theme=1") and safe_href("#top") and safe_href("https://example.com")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cli.py tests/test_citations.py -q`
Expected: FAIL (web exits 2 without keys; `safe_href("//evil.com/x")` is true)

- [ ] **Step 3: Write the implementation**

`pulse/citations.py`, `safe_href`:

```python
def safe_href(value: str) -> bool:
    """True for http(s), mailto, site-relative (/...) and fragment (#...) links.
    Protocol-relative //host and /\\host are rejected: browsers treat them as another site."""
    url = _URL_JUNK_RE.sub("", value).lower()
    if url.startswith(("//", "/\\")):
        return False
    return url.startswith(_SAFE_SCHEMES) or url.startswith(("/", "#"))
```

`pulse/web/settings.py`: add `agents_off_reason: str | None = None` after `http_client_factory`, and:

```python
    @property
    def agents_off_text(self) -> str:
        if self.demo:
            return "Agents are off in demo mode"
        return self.agents_off_reason or "Agents are off"
```

`pulse/run.py`: add `from pulse.config import missing_keys`, the constant

```python
MODEL_COMMANDS = ("triage", "pipeline", "themes", "digest", "investigate")
```

change the config load in `main` to `config = load_config(args.config, require_keys=args.command in MODEL_COMMANDS)`, change `_serve` to

```python
def _serve(settings: WebSettings, host: str, port: int) -> int:
    if _is_loopback(host):
        bound = host.strip("[]")
        extra = (bound, f"[{bound}]") if ":" in bound else (bound,)
        settings = replace(settings, allowed_hosts=tuple(dict.fromkeys(LOCAL_HOSTS + extra)))
    else:
        print(f"Serving on {host} with no login: anyone on your network can read the dashboard", file=sys.stderr)
    print(f"Discord Pulse dashboard on http://{host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0
```

and the `web` branch to

```python
    elif args.command == "web":
        conn.close()
        missing = missing_keys(config)
        settings = WebSettings(
            db_path=Path(args.db) if args.db else config.db_path, config=config,
            server_name=args.name or "Discord server",
            llm_factory=None if missing else build_llm,
            agents_off_reason=f"Agents are off: set {', '.join(missing)}" if missing else None,
        )
        return _serve(settings, args.host, args.port)
```

In `README.md`'s Dashboard section, add one sentence: "`web` starts without provider keys; the agent buttons are then off and say which key to set."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_cli.py tests/test_citations.py -q` then `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 5: Commit**

```bash
git add pulse/run.py pulse/web/settings.py pulse/citations.py README.md tests/test_cli.py tests/test_citations.py
git commit -m "fix: web starts without keys, trusts any bound loopback host, rejects //host links

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```
