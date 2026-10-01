"""Investigate agent: answers "why" questions with read-only tools over the database."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from pulse import stats
from pulse.agents.base import BudgetExceeded, LLMError, ToolCall, ToolError, ToolSpec
from pulse.agents.llm import LLMClient
from pulse.citations import cited_ids, strip_unknown
from pulse.models import KINDS, to_iso

MAX_TOOL_CALLS = 12
SEARCH_LIMIT_MAX = 50
THREAD_LIMIT = 30
_CLIP = 300
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_DATE = {"type": "string", "description": "YYYY-MM-DD (UTC)"}

TOOLS = [
    ToolSpec(
        "query_stats",
        "Aggregate statistics for a date range [start, end). metric: period_summary (message count, average "
        "sentiment, negatives, needs-reply, counts by kind), sentiment_series (per day), theme_scores (pain "
        "points ranked, for the window ending at end), queue_counts (open mod queue items). Defaults: end = "
        "tomorrow, start = 7 days before end.",
        {"type": "object", "properties": {
            "metric": {"type": "string", "enum": ["period_summary", "sentiment_series", "theme_scores", "queue_counts"]},
            "start": _DATE, "end": _DATE,
        }, "required": ["metric"]},
    ),
    ToolSpec(
        "search_messages",
        "Find triaged community messages, newest first. All filters are optional. Each result has a "
        "message_id you can cite.",
        {"type": "object", "properties": {
            "text": {"type": "string", "description": "case-insensitive substring of the message text"},
            "theme_id": {"type": "integer"},
            "author_id": {"type": "string"},
            "kind": {"type": "string", "enum": list(KINDS)},
            "start": _DATE, "end": _DATE,
            "negative_only": {"type": "boolean"},
            "limit": {"type": "integer", "minimum": 1, "maximum": SEARCH_LIMIT_MAX},
        }},
    ),
    ToolSpec(
        "get_thread",
        "The conversation around one message: the message, its thread (or the thread started from it) and "
        "direct replies, oldest first.",
        {"type": "object", "properties": {"message_id": {"type": "string"}}, "required": ["message_id"]},
    ),
]


def _clip(text: str) -> str:
    return text if len(text) <= _CLIP else text[:_CLIP] + "…"


class Toolbox:
    def __init__(self, conn: sqlite3.Connection, now: datetime):
        self.conn = conn
        self.now = now
        self.seen: set[str] = set()

    def execute(self, call: ToolCall) -> str:
        handlers = {
            "query_stats": self.query_stats,
            "search_messages": self.search_messages,
            "get_thread": self.get_thread,
        }
        handler = handlers.get(call.name)
        if handler is None:
            raise ToolError(f"unknown tool {call.name!r}")
        if not isinstance(call.arguments, dict):
            raise ToolError("arguments must be an object")
        return json.dumps(handler(call.arguments), ensure_ascii=False, default=str)

    def _tomorrow(self) -> datetime:
        return (self.now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)

    def _date(self, args: dict, key: str, default: datetime) -> datetime:
        value = args.get(key)
        if value in (None, ""):
            return default
        try:
            return datetime.combine(date.fromisoformat(str(value)), time.min, timezone.utc)
        except ValueError as e:
            raise ToolError(f"{key} must be YYYY-MM-DD, got {value!r}") from e

    def _range(self, args: dict, default_start: datetime | None = None) -> tuple[datetime, datetime]:
        end = self._date(args, "end", self._tomorrow())
        start = self._date(args, "start", default_start if default_start is not None else end - timedelta(days=7))
        if start >= end:
            raise ToolError("start must be before end")
        return start, end

    def _int(self, args: dict, key: str) -> int | None:
        value = args.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolError(f"{key} must be an integer, got {value!r}")
        return value

    def query_stats(self, args: dict):
        metric = args.get("metric")
        start, end = self._range(args)
        if metric == "period_summary":
            return stats.period_summary(self.conn, start, end)
        if metric == "sentiment_series":
            return stats.sentiment_series(self.conn, start, end)
        if metric == "theme_scores":
            window = max(1, (end - start).days)
            return [asdict(s) for s in stats.theme_scores(self.conn, end, window_days=window)]
        if metric == "queue_counts":
            return stats.queue_counts(self.conn)
        raise ToolError(f"unknown metric {metric!r}")

    def search_messages(self, args: dict):
        start, end = self._range(args, default_start=_EPOCH)
        limit = self._int(args, "limit")
        limit = 20 if limit is None else max(1, min(limit, SEARCH_LIMIT_MAX))
        kind = args.get("kind")
        if kind is not None and kind not in KINDS:
            raise ToolError(f"kind must be one of {list(KINDS)}")
        sql = (
            f"SELECT DISTINCT {stats.MESSAGE_COLUMNS}, m.author_id FROM messages m"
            " JOIN triage t ON t.message_id = m.id"
        )
        params: list = []
        theme_id = self._int(args, "theme_id")
        if theme_id is not None:
            ids = stats.theme_member_ids(self.conn, theme_id)
            if not ids:
                return []
            sql += f" JOIN message_themes mt ON mt.message_id = m.id AND mt.theme_id IN ({','.join('?' * len(ids))})"
            params += ids
        sql += " WHERE m.is_team = 0 AND m.is_bot = 0 AND m.created_at >= ? AND m.created_at < ?"
        params += [to_iso(start), to_iso(end)]
        if args.get("text"):
            sql += " AND lower(m.content) LIKE ? ESCAPE '\\'"
            params.append(stats._like(str(args["text"]).lower()))
        if args.get("author_id"):
            sql += " AND m.author_id = ?"
            params.append(str(args["author_id"]))
        if kind is not None:
            sql += " AND t.kind = ?"
            params.append(kind)
        if args.get("negative_only") is True:
            sql += " AND t.sentiment < 0"
        sql += " ORDER BY m.created_at DESC, m.id DESC LIMIT ?"
        params.append(limit)
        results = []
        for r in self.conn.execute(sql, params):
            item = stats.to_message(r)
            item["content"] = _clip(item["content"])
            item["author_id"] = r["author_id"]
            results.append(item)
        self.seen.update(m["message_id"] for m in results)
        return results

    def get_thread(self, args: dict):
        mid = args.get("message_id")
        if not isinstance(mid, str) or not mid:
            raise ToolError("message_id is required")
        root = self.conn.execute("SELECT id, thread_id FROM messages WHERE id = ?", (mid,)).fetchone()
        if root is None:
            raise ToolError(f"no message {mid!r}")
        thread = root["thread_id"] or mid
        rows = self.conn.execute(
            "SELECT m.id, m.author_name, m.is_team, m.is_bot, m.created_at, m.content FROM messages m"
            " WHERE m.id = ? OR m.thread_id = ? OR m.reply_to_id = ?"
            " ORDER BY m.created_at, m.id LIMIT ?",
            (mid, thread, mid, THREAD_LIMIT),
        ).fetchall()
        results = [
            {"message_id": r["id"], "author": r["author_name"], "is_team": bool(r["is_team"]),
             "is_bot": bool(r["is_bot"]), "created_at": r["created_at"], "content": _clip(r["content"])}
            for r in rows
        ]
        self.seen.update(m["message_id"] for m in results)
        return results


@dataclass(frozen=True)
class InvestigationResult:
    investigation_id: int
    markdown: str
    cited_message_ids: list[str]
    removed_citations: list[str]
    tool_calls: int


def load_prompt() -> str:
    return (Path(__file__).parent / "prompts" / "investigate_v1.md").read_text(encoding="utf-8")


def run_investigation(
    conn: sqlite3.Connection, llm: LLMClient, question: str, now: datetime, *, context: dict | None = None
) -> InvestigationResult:
    question = question.strip()
    if not question:
        raise ValueError("question is empty")
    context = context or {}
    with conn:
        inv_id = int(conn.execute(
            "INSERT INTO investigations (question, context, created_at) VALUES (?, ?, ?)",
            (question, json.dumps(context), to_iso(now)),
        ).lastrowid)
    toolbox = Toolbox(conn, now)
    user = json.dumps({"question": question, "context": context, "today": now.date().isoformat()})
    try:
        resp = llm.run_tools("investigate", load_prompt(), user, TOOLS, toolbox.execute, max_calls=MAX_TOOL_CALLS)
    except (BudgetExceeded, LLMError) as e:
        with conn:
            conn.execute(
                "UPDATE investigations SET markdown = ? WHERE id = ?", (f"Investigation failed: {e}", inv_id)
            )
        raise
    except Exception as e:
        with conn:
            conn.execute(
                "UPDATE investigations SET markdown = ? WHERE id = ?",
                (f"Investigation failed: {type(e).__name__}: {e}", inv_id),
            )
        raise
    markdown, removed = strip_unknown(resp.text, toolbox.seen)
    cited = cited_ids(markdown)
    with conn:
        conn.execute(
            "UPDATE investigations SET markdown = ?, cited_message_ids = ?, run_id = ? WHERE id = ?",
            (markdown, json.dumps(cited), resp.run_id, inv_id),
        )
    return InvestigationResult(inv_id, markdown, cited, removed, resp.tool_calls)
