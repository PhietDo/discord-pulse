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
