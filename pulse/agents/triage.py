"""Triage subagent: label every message with sentiment, kind, topics, needs_reply.

Batches fan out to concurrent LLM calls. Worker threads only call the model;
triage rows are written on the calling thread after all calls finish.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def run_triage(
    conn: sqlite3.Connection,
    llm: LLMClient,
    *,
    batch_size: int = BATCH_SIZE,
    concurrency: int = 4,
    since: datetime | None = None,
    force: bool = False,
) -> TriageStats:
    rows = select_untriaged(conn, since, force=force)
    batches = [rows[i : i + batch_size] for i in range(0, len(rows), batch_size)]
    jobs = [({r["id"] for r in b}, build_batch_input(conn, b)) for b in batches]
    system = load_prompt()

    # Set once any batch hits the budget cap, so later batches skip the call
    # entirely instead of each recording their own skipped_budget row.
    stop = threading.Event()

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

    stats = TriageStats()
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
