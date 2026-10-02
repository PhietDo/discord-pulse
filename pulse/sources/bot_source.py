"""Read-only Discord bot ingest, free of discord.py so it is tested with fakes.

The bot only reads: it never posts, reacts, edits or deletes. bot_client.py holds the
discord.py glue and is the only module that imports discord.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterator

from pulse.config import Config
from pulse.models import Message, from_iso
from pulse.store import upsert_messages

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
        except sqlite3.Error as e:
            with self._pending_lock:
                for mid, m in batch.items():
                    self._pending.setdefault(mid, m)  # a newer version that arrived meanwhile wins
            self.flush_failures += 1
            if "locked" in str(e):
                return 0
            raise
        self.written += len(batch)
        return len(batch)
