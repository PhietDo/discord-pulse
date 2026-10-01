"""Theme agent: group labelled messages into recurring themes (pain points and wins).

Stage A: Jev picks an existing theme per message when it is confident.
Stage B: the theme LLM proposes assignments, new themes, merges and renames for
the rest, one batch at a time so each batch sees the themes the last one made.
Plain code applies proposals with guardrails (pulse.themes).
"""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pulse.agents.base import BudgetExceeded, LLMError
from pulse.agents.classifier import theme_question
from pulse.agents.llm import LLMClient
from pulse.themes import ThemeBudget, active_themes, apply_proposal, assign, mark_themed

SCHEMA_NAME = "theme_result"
BATCH_SIZE = 60
MAX_CANDIDATES = 600
MAX_THEMES_FOR_JEV = 40
MAX_CONTENT_CHARS = 500

_STR_LIST = {"type": "array", "items": {"type": "string"}}
_INT_LIST = {"type": "array", "items": {"type": "integer"}}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props), "properties": props}


THEME_SCHEMA = _obj({
    "assignments": {"type": "array", "items": _obj({"message_id": {"type": "string"}, "theme_ids": _INT_LIST})},
    "new_themes": {"type": "array", "items": _obj({
        "name": {"type": "string"}, "description": {"type": "string"}, "message_ids": _STR_LIST,
    })},
    "merges": {"type": "array", "items": _obj({
        "from_id": {"type": "integer"}, "into_id": {"type": "integer"}, "reason": {"type": "string"},
    })},
    "renames": {"type": "array", "items": _obj({
        "theme_id": {"type": "integer"}, "name": {"type": "string"}, "description": {"type": "string"},
    })},
})


@dataclass
class ThemeStats:
    considered: int = 0
    jev_assigned: int = 0
    llm_batches: int = 0
    failed_batches: int = 0
    skipped_budget: bool = False
    created: int = 0
    assigned: int = 0
    merged: int = 0
    renamed: int = 0
    rejected: list[str] = field(default_factory=list)


def load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "theme_v1.md").read_text(encoding="utf-8")


def select_candidates(conn: sqlite3.Connection, limit: int = MAX_CANDIDATES) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT m.id, m.content, t.kind, t.sentiment, t.topics FROM messages m"
        " JOIN triage t ON t.message_id = m.id"
        " WHERE t.themed_at IS NULL AND t.topics != '[]' AND m.is_team = 0 AND m.is_bot = 0"
        " ORDER BY m.created_at DESC, m.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def _state(row: sqlite3.Row) -> dict:
    content = row["content"]
    return {
        "message_id": row["id"],
        "content": content if len(content) <= MAX_CONTENT_CHARS else content[:MAX_CONTENT_CHARS] + "…",
        "kind": row["kind"],
        "sentiment": row["sentiment"],
        "topics": json.loads(row["topics"]),
    }


def validate_proposal(data: dict, message_ids: set[str], theme_ids: set[int]) -> None:
    bad_messages = sorted(
        ({a["message_id"] for a in data["assignments"]}
         | {m for t in data["new_themes"] for m in t["message_ids"]})
        - message_ids
    )
    bad_themes = sorted(
        ({tid for a in data["assignments"] for tid in a["theme_ids"]}
         | {m["from_id"] for m in data["merges"]} | {m["into_id"] for m in data["merges"]}
         | {r["theme_id"] for r in data["renames"]})
        - theme_ids
    )
    if bad_messages or bad_themes:
        raise ValueError(f"unknown message ids {bad_messages}, unknown theme ids {bad_themes}")


def _jev_stage(conn, llm, rows, themes, concurrency, stats, now) -> list[sqlite3.Row]:
    cfg = llm.config.classifier
    question = theme_question(themes[:MAX_THEMES_FOR_JEV])
    states = [_state(r) for r in rows]
    by_id = {r["id"]: r for r in rows}

    def call(state):
        try:
            return state["message_id"], llm.choose(state, question), None
        except BudgetExceeded:
            return state["message_id"], None, "budget"
        except LLMError:
            return state["message_id"], None, "failed"

    leftovers = []
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for fut in as_completed([pool.submit(call, s) for s in states]):
            mid, resp, error = fut.result()
            if error == "budget":
                stats.skipped_budget = True
                continue
            if error is None and resp.result.choice != "none" and resp.result.confidence >= cfg.min_confidence:
                with llm.db_lock, conn:
                    assign(conn, mid, int(resp.result.choice))
                    mark_themed(conn, [mid], now)
                stats.jev_assigned += 1
                continue
            leftovers.append(by_id[mid])
    leftovers.sort(key=lambda r: r["id"])
    return leftovers


def run_themes(conn: sqlite3.Connection, llm: LLMClient, now: datetime, *, concurrency: int = 8) -> ThemeStats:
    stats = ThemeStats()
    rows = select_candidates(conn)
    stats.considered = len(rows)
    if not rows:
        return stats

    themes = active_themes(conn)
    cfg = llm.config.classifier
    if themes and cfg is not None and cfg.enabled and llm.has_classifier:
        rows = _jev_stage(conn, llm, rows, themes, concurrency, stats, now)
        if stats.skipped_budget:
            return stats

    budget = ThemeBudget()
    system = load_prompt()
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        ids = {r["id"] for r in batch}
        current = active_themes(conn)
        theme_ids = {t["id"] for t in current}
        user = json.dumps({
            "themes": [{"id": t["id"], "name": t["name"], "description": t["description"]} for t in current],
            "messages": [_state(r) for r in batch],
        }, ensure_ascii=False)
        try:
            resp = llm.complete(
                "theme", system, user, THEME_SCHEMA, SCHEMA_NAME,
                validate=lambda data: validate_proposal(data, ids, theme_ids),
            )
        except BudgetExceeded:
            stats.skipped_budget = True
            break
        except LLMError:
            stats.failed_batches += 1
            continue
        stats.llm_batches += 1
        with llm.db_lock, conn:
            changes = apply_proposal(conn, resp.data, now, resp.run_id, budget)
            mark_themed(conn, ids, now)
        stats.created += changes.created
        stats.assigned += changes.assigned
        stats.merged += changes.merged
        stats.renamed += changes.renamed
        stats.rejected += changes.rejected
    return stats
