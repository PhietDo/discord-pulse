"""Message citations in agent markdown: [[msg:<message_id>]]."""
from __future__ import annotations

import re
import sqlite3

from pulse.links import jump_link

CITATION_RE = re.compile(r"\[\[msg:([^\]\s]+)\]\]")
# Near-misses models write: [[msg:<id>]] and [[msg: id ]].
_SLOPPY_RE = re.compile(r"\[\[msg:\s*<?\s*([^\]\s<>]+)\s*>?\s*\]\]")
# Any other [[msg:...]] token left after strict parsing.
_LENIENT_RE = re.compile(r"\[\[msg:([^\]]*)\]\]")


def cited_ids(markdown: str) -> list[str]:
    seen: list[str] = []
    for mid in CITATION_RE.findall(markdown):
        if mid not in seen:
            seen.append(mid)
    return seen


def strip_unknown(markdown: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Remove citations to messages the agent was not shown, and malformed citation tokens.

    Near-miss forms are normalized to [[msg:<id>]] first. Returns the cleaned markdown and
    the removed ids (or malformed token contents), deduped in order.
    """
    removed: list[str] = []

    def drop(value: str) -> str:
        if value not in removed:
            removed.append(value)
        return ""

    def replace(match: re.Match) -> str:
        mid = match.group(1)
        return match.group(0) if mid in allowed else drop(mid)

    def sweep(match: re.Match) -> str:
        if CITATION_RE.fullmatch(match.group(0)):
            return match.group(0)
        return drop(match.group(1).strip())

    markdown = _SLOPPY_RE.sub(r"[[msg:\1]]", markdown)
    markdown = CITATION_RE.sub(replace, markdown)
    return _LENIENT_RE.sub(sweep, markdown), removed


def render_text(markdown: str, conn: sqlite3.Connection) -> str:
    """Plain-text rendering for the CLI: each citation becomes the author and a jump link."""

    def replace(match: re.Match) -> str:
        row = conn.execute(
            "SELECT guild_id, channel_id, author_name FROM messages WHERE id = ?", (match.group(1),)
        ).fetchone()
        if row is None:
            return "[missing message]"
        return f"({row['author_name']}, {jump_link(row['guild_id'], row['channel_id'], match.group(1))})"

    return CITATION_RE.sub(replace, markdown)
