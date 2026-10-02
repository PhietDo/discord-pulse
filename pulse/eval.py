"""Accuracy check for triage: score models against a hand-labelled gold set.

    python -m pulse.eval [--model provider:model ...] [--gold eval/gold.jsonl]
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from pulse.agents.base import Backend, BudgetExceeded, LLMError
from pulse.agents.classifier import Classifier
from pulse.agents.llm import LLMClient
from pulse.agents.triage import build_batch_input, run_triage, select_untriaged
from pulse.config import (
    CLASSIFIER_PROVIDERS, KEY_ENV, PROVIDERS, ClassifierConfig, Config, ConfigError, ModelRef, load_config,
)
from pulse.db import connect
from pulse.models import KINDS, Message, parse_timestamp
from pulse.pipeline import build_backends
from pulse.store import upsert_messages

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
    with Path(path).open(encoding="utf-8-sig") as fh:
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
    """Write n random community (non-staff) messages with empty labels, for hand labelling.
    Raises LookupError, writing nothing, when the database has no such messages."""
    out = Path(out)
    if out.exists():
        raise FileExistsError(f"{out} already exists; pick another --out")
    rows = conn.execute(
        "SELECT * FROM messages WHERE is_bot = 0 AND is_team = 0 AND trim(content) != '' ORDER BY id"
    ).fetchall()
    if not rows:
        raise LookupError("no messages")
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
        except LookupError:
            print(f"eval: no messages in {config.db_path}; run ingest first", file=sys.stderr)
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
