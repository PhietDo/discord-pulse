"""Message citations in agent markdown: [[msg:<message_id>]]."""
from __future__ import annotations

import re
import sqlite3

from pulse.links import jump_link

CITATION_RE = re.compile(r"\[\[msg:([^\]\s]+)\]\]")


def cited_ids(markdown: str) -> list[str]:
    seen: list[str] = []
    for mid in CITATION_RE.findall(markdown):
        if mid not in seen:
            seen.append(mid)
    return seen


def strip_unknown(markdown: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Remove citations to messages the agent was not shown."""
    removed: list[str] = []

    def replace(match: re.Match) -> str:
        mid = match.group(1)
        if mid in allowed:
            return match.group(0)
        removed.append(mid)
        return ""

    return CITATION_RE.sub(replace, markdown), removed


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
