"""Triage: label every message with sentiment, kind, topics, needs_reply.

With the classifier enabled, Stage A asks Jev about every message and stores
confident, low-stakes labels directly; Stage B sends the rest to the batched
LLM path. Worker threads only call models; the calling thread writes each
result as it arrives, under the LLM client's DB lock.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from pulse.agents.base import BudgetExceeded, LLMError
from pulse.agents.classifier import QUESTIONS_VERSION, ClassifierResult
from pulse.agents.llm import ClassifyResponse, LLMClient, LLMResponse
from pulse.config import ClassifierConfig
from pulse.models import KINDS, TriageResult, to_iso

PROMPT_VERSION = "triage-v2"
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
    jev_labeled: int = 0
    escalated: int = 0
    classifier_failed: int = 0
    kept_llm: int = 0
    left_untriaged: int = 0


def load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "triage_v2.md").read_text(encoding="utf-8")


def select_untriaged(
    conn: sqlite3.Connection, since: datetime | None = None, force: bool = False
) -> list[sqlite3.Row]:
    sql = (
        "SELECT m.* FROM messages m LEFT JOIN triage t ON t.message_id = m.id"
        " WHERE m.is_bot = 0 AND trim(m.content) != ''"
    )
    if not force:
        sql += " AND t.message_id IS NULL"
    params: list[str] = []
    if since is not None:
        sql += " AND m.created_at >= ?"
        params.append(to_iso(since))
    sql += " ORDER BY m.created_at DESC, m.id DESC"
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

    # Messages that already carry LLM-assigned topics: a force re-triage must not
    # blow those away with a Jev-only row that has topics = [].
    keep: set[str] = set()
    ids = list(by_id)
    if ids:
        placeholders = ",".join("?" for _ in ids)
        existing = conn.execute(
            f"SELECT message_id, topics FROM triage WHERE message_id IN ({placeholders}) AND labeler = 'llm'",
            ids,
        ).fetchall()
        keep = {r["message_id"] for r in existing if r["topics"] != "[]"}

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
    rule_escalated = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = [pool.submit(call, s) for s in states]
        for fut in as_completed(futures):
            mid, resp, error = fut.result()
            row = by_id[mid]
            if error == "budget":
                continue
            if error == "failed":
                stats.classifier_failed += 1
                if row["is_team"]:
                    if mid in keep:
                        continue
                    now = to_iso(datetime.now(timezone.utc))
                    with llm.db_lock, conn:
                        conn.execute(_INSERT, (
                            mid, 0, 1.0, "other", "[]", 0, "staff-rule",
                            None, now, 0.0, None, "rule",
                        ))
                    stats.triaged += 1
                    continue
                escalate.append(row)
                continue
            result = _staff_override(resp.result) if row["is_team"] else resp.result
            p_by_id[mid] = result.needs_reply_p
            if not row["is_team"] and needs_escalation(result, cfg):
                escalate.append(row)
                rule_escalated += 1
                continue
            if mid in keep:
                stats.kept_llm += 1
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
    stats.escalated = rule_escalated
    return escalate, p_by_id


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
        total_selected = len(rows)
        rows, p_by_id = _classify_stage(conn, llm, rows, cfg, classify_concurrency, stats, stop)
        if stop.is_set():
            # Unfinished messages stay untriaged and are retried on the next run.
            stats.skipped_budget_batches += 1
            stats.left_untriaged = total_selected - stats.triaged
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
                stats.left_untriaged += len(ids)
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
