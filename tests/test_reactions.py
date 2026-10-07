import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from pulse.config import ConfigError, load_config
from pulse.db import connect
from pulse.models import merge_reactions
from pulse.sources.bot_source import message_from_discord
from pulse.sources.file_source import FileSource
from pulse.store import upsert_messages
from tests.fakes import T0, msg


def export(tmp_path, message_extra):
    folder = tmp_path / "imports"
    folder.mkdir(exist_ok=True)
    message = {"id": "1", "type": "Default", "timestamp": "2026-09-28T12:00:00+00:00",
               "content": "add a Django guide", "author": {"id": "u1", "name": "alice", "isBot": False}}
    message.update(message_extra)
    (folder / "help.json").write_text(json.dumps({
        "guild": {"id": "900", "name": "Acme"},
        "channel": {"id": "100", "type": "GuildTextChat", "name": "help"},
        "messages": [message],
    }), encoding="utf-8")
    return folder


def rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT message_id, emoji, count FROM reactions ORDER BY count DESC, emoji")]


def test_merge_reactions_sums_and_drops_junk():
    assert merge_reactions([("👍", 3), ("pepe", 1), ("👍", 2), ("", 5), ("x", 0), (None, 1), ("y", "bad")]) == (
        ("👍", 5), ("pepe", 1))


def test_file_import_stores_reaction_counts(tmp_path):
    source = FileSource(export(tmp_path, {"reactions": [
        {"emoji": {"id": "", "name": "👍", "code": "thumbsup"}, "count": 3},
        {"emoji": {"id": "123", "name": "pepe", "code": "pepe"}, "count": 1},
        {"emoji": {"name": "👍"}, "count": 2},
    ]}))
    [m] = list(source.fetch())
    assert source.errors == [] and m.reactions == (("👍", 5), ("pepe", 1))
    conn = connect(":memory:")
    upsert_messages(conn, [m], frozenset())
    assert rows(conn) == [("1", "👍", 5), ("1", "pepe", 1)]


def test_export_without_reactions_has_none(tmp_path):
    [m] = list(FileSource(export(tmp_path, {})).fetch())
    assert m.reactions == ()


def test_reimport_replaces_counts():
    conn = connect(":memory:")
    m = replace(msg("1"), reactions=(("👍", 2),))
    upsert_messages(conn, [m], frozenset())
    upsert_messages(conn, [replace(m, reactions=(("👍", 7), ("🎉", 1)))], frozenset())
    assert rows(conn) == [("1", "👍", 7), ("1", "🎉", 1)]
    upsert_messages(conn, [replace(m, reactions=())], frozenset())
    assert rows(conn) == []


def test_bot_mapping_reads_reactions():
    raw = NS(
        id=5, type=NS(name="default"), channel=NS(id=100, name="help"), guild=NS(id=900), content="hi",
        created_at=T0, edited_at=None, reference=None,
        author=NS(id=1, name="a", display_name="A", bot=False, display_avatar=None),
        reactions=[NS(emoji=NS(name="👍"), count=4), NS(emoji="🎉", count=1), NS(emoji=NS(name="x"), count=0)],
    )
    assert message_from_discord(raw, "900").reactions == (("👍", 4), ("🎉", 1))


BASE = (
    '[server]\nguild_id = "1"\n{tz}\n[models]\ntriage = "anthropic:m"\ntheme = "anthropic:m"\n'
    'digest = "anthropic:m"\ninvestigate = "anthropic:m"\n[pricing."anthropic:m"]\ninput = 1\noutput = 2\n'
)


def test_server_timezone(tmp_path):
    path = tmp_path / "pulse.toml"
    path.write_text(BASE.format(tz=""))
    assert load_config(path, {}, require_keys=False).timezone == "UTC"
    path.write_text(BASE.format(tz='timezone = "America/New_York"'))
    assert load_config(path, {}, require_keys=False).timezone == "America/New_York"
    path.write_text(BASE.format(tz='timezone = "Mars/Olympus"'))
    with pytest.raises(ConfigError, match="timezone"):
        load_config(path, {}, require_keys=False)
