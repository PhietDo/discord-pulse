"""Import Discord exports dropped into the imports folder.

Supports DiscordChatExporter JSON and a simple CSV. How the export files are
produced is outside this tool; it never reads Discord with a user token.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Iterator

from pulse.models import Message, merge_reactions, parse_timestamp

CSV_REQUIRED = (
    "guild_id", "channel_id", "message_id", "author_id", "author_name", "content", "created_at",
)
DCE_MESSAGE_TYPES = ("Default", "Reply")


class FileSource:
    def __init__(self, imports_dir: Path):
        self.imports_dir = Path(imports_dir)
        self.errors: list[str] = []

    def fetch(self, since: datetime | None = None) -> Iterator[Message]:
        self.errors = []
        if not self.imports_dir.is_dir():
            self.errors.append(f"imports folder {self.imports_dir} not found")
            return
        for path in sorted(self.imports_dir.iterdir()):
            if path.is_dir() or path.name.startswith("."):
                continue
            suffix = path.suffix.lower()
            if suffix == ".json":
                messages = self._read_json(path)
            elif suffix == ".csv":
                messages = self._read_csv(path)
            else:
                self.errors.append(f"{path.name}: unsupported format (export as JSON or CSV)")
                continue
            for m in messages:
                if since is None or m.created_at >= since:
                    yield m

    def _read_json(self, path: Path) -> list[Message]:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            guild_id = str(data["guild"]["id"])
            channel = data["channel"]
            channel_id = str(channel["id"])
            raw_messages = data["messages"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            self.errors.append(f"{path.name}: cannot read JSON: {e}")
            return []
        except (KeyError, TypeError) as e:
            self.errors.append(f"{path.name}: not a DiscordChatExporter JSON export (missing {e})")
            return []

        is_thread = "Thread" in str(channel.get("type", ""))
        channel_name = str(channel.get("name") or "")
        parent_channel_id = str(channel["categoryId"]) if is_thread and channel.get("categoryId") else None
        out: list[Message] = []
        for i, raw in enumerate(raw_messages):
            try:
                if raw.get("type", "Default") not in DCE_MESSAGE_TYPES:
                    continue
                author = raw["author"]
                avatar = author.get("avatarUrl")
                reference = raw.get("reference") or {}
                edited = raw.get("timestampEdited")
                out.append(
                    Message(
                        id=str(raw["id"]),
                        guild_id=guild_id,
                        channel_id=channel_id,
                        channel_name=channel_name,
                        thread_id=channel_id if is_thread else None,
                        author_id=str(author["id"]),
                        author_name=str(author.get("nickname") or author["name"]),
                        author_avatar_url=avatar if isinstance(avatar, str) and avatar.startswith("http") else None,
                        is_bot=bool(author.get("isBot", False)),
                        content=str(raw.get("content") or ""),
                        created_at=parse_timestamp(raw["timestamp"]),
                        edited_at=parse_timestamp(edited) if edited else None,
                        reply_to_id=str(reference["messageId"]) if reference.get("messageId") else None,
                        source="file",
                        parent_channel_id=parent_channel_id,
                        reactions=merge_reactions(
                            ((r.get("emoji") or {}).get("name") or (r.get("emoji") or {}).get("code"), r.get("count"))
                            for r in raw.get("reactions") or [] if isinstance(r, dict)
                        ),
                    )
                )
            except (AttributeError, KeyError, TypeError, ValueError) as e:
                self.errors.append(f"{path.name}: message #{i}: {e!r}")
        return out

    def _read_csv(self, path: Path) -> list[Message]:
        out: list[Message] = []
        try:
            with path.open(newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                missing = [c for c in CSV_REQUIRED if c not in (reader.fieldnames or [])]
                if missing:
                    self.errors.append(f"{path.name}: missing required columns {missing}")
                    return []
                for row in reader:
                    try:
                        out.append(_csv_row(row))
                    except ValueError as e:
                        self.errors.append(f"{path.name}:{reader.line_num}: {e}")
        except (OSError, UnicodeDecodeError, csv.Error) as e:
            self.errors.append(f"{path.name}: {e}")
            return []
        return out


def _csv_row(row: dict[str, str | None]) -> Message:
    def val(key: str) -> str | None:
        return (row.get(key) or "").strip() or None

    for column in CSV_REQUIRED:
        if column != "content" and not val(column):
            raise ValueError(f"missing {column}")
    edited = val("edited_at")
    return Message(
        id=val("message_id"),
        guild_id=val("guild_id"),
        channel_id=val("channel_id"),
        channel_name=val("channel_name") or "",
        thread_id=val("thread_id"),
        author_id=val("author_id"),
        author_name=val("author_name"),
        author_avatar_url=val("author_avatar_url"),
        is_bot=(val("is_bot") or "").lower() in ("1", "true", "yes"),
        content=row.get("content") or "",
        created_at=parse_timestamp(val("created_at")),
        edited_at=parse_timestamp(edited) if edited else None,
        reply_to_id=val("reply_to_id"),
        source="file",
        parent_channel_id=val("parent_channel_id"),
    )
