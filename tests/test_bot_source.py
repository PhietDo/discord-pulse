import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

from pulse.db import connect
from pulse.pipeline import ingest
from pulse.sources.bot_source import (
    BOT_PERMISSIONS, BotSource, backfill, invite_url, last_seen, message_from_discord, wanted,
)
from pulse.store import upsert_messages
from tests.fakes import make_config, msg

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)
GUILD = NS(id=900, name="Acme")


class Forbidden(Exception):
    pass


class FakeChannel:
    def __init__(self, id, name, *, fail=None, threads=(), archived=(), parent_id=None):
        self.id, self.name, self.parent_id = id, name, parent_id
        self.messages, self.threads, self.archived, self.fail = [], list(threads), list(archived), fail
        self.history_calls = []

    async def history(self, *, after=None, oldest_first=True, limit=None):
        self.history_calls.append(after)
        if self.fail:
            raise self.fail
        for m in sorted(self.messages, key=lambda m: m.created_at):
            if after is None or m.created_at > after:
                yield m

    async def archived_threads(self, *, limit=None):
        for t in self.archived:
            yield t


class FakeForum:
    def __init__(self, id, name, threads=()):
        self.id, self.name, self.threads = id, name, list(threads)

    async def archived_threads(self, *, limit=None):
        return
        yield


def post(channel, id, content="hi", *, minutes=0, author_id=1, kind="default", ref=None, bot=False, naive=False):
    created = T0 + timedelta(minutes=minutes)
    m = NS(
        id=id, type=NS(name=kind), channel=channel, guild=GUILD, content=content,
        created_at=created.replace(tzinfo=None) if naive else created, edited_at=None,
        reference=NS(message_id=ref) if ref else None,
        author=NS(id=author_id, name=f"user{author_id}", display_name=f"User {author_id}", bot=bot,
                  display_avatar=NS(url=f"https://cdn/{author_id}.png")),
    )
    channel.messages.append(m)
    return m


def test_message_mapping_matches_file_import():
    help_ = FakeChannel(100, "help")
    thread = FakeChannel(300, "M1 install", parent_id=100)
    m = message_from_discord(post(help_, 1, "install fails", ref=7, naive=True), "900")
    assert (m.id, m.guild_id, m.channel_id, m.channel_name, m.thread_id, m.parent_channel_id) == \
        ("1", "900", "100", "help", None, None)
    assert m.author_name == "User 1" and m.author_avatar_url == "https://cdn/1.png"
    assert m.reply_to_id == "7" and m.source == "bot" and m.created_at.tzinfo is not None
    t = message_from_discord(post(thread, 2, "same here", bot=True), "900")
    assert (t.channel_id, t.thread_id, t.parent_channel_id, t.is_bot) == ("300", "300", "100", True)
    assert message_from_discord(post(help_, 3, kind="pins_add"), "900") is None
    blank = post(help_, 4)
    blank.content = None
    assert message_from_discord(blank, "900").content == ""


def test_wanted_respects_channel_ids_and_thread_parents():
    assert wanted(make_config(), "5")
    cfg = make_config(channel_ids=("100",))
    assert wanted(cfg, "100") and wanted(cfg, "300", "100")
    assert not wanted(cfg, "200") and not wanted(cfg, "300", "200")


def guild_with_everything():
    help_ = FakeChannel(100, "help")
    old = post(help_, 1, "old", minutes=0)
    post(help_, 2, "new", minutes=10)
    active = FakeChannel(300, "M1 install", parent_id=100)
    post(active, 3, "in thread", minutes=5)
    archived = FakeChannel(301, "old thread", parent_id=100)
    post(archived, 4, "archived reply", minutes=6)
    help_.threads, help_.archived = [active], [archived, active]
    general = FakeChannel(200, "general")
    post(general, 5, "chit chat", minutes=1)
    forum_thread = FakeChannel(401, "How do I deploy?", parent_id=400)
    post(forum_thread, 6, "forum question", minutes=2)
    forum = FakeForum(400, "help-forum", threads=[forum_thread])
    guild = NS(id=900, name="Acme", text_channels=[help_, general], forums=[forum])
    return guild, help_, general, old


def test_backfill_reads_channels_threads_and_forums_since_last_seen():
    guild, help_, general, old = guild_with_everything()
    errors = []
    since = {"100": old.created_at}
    out = asyncio.run(backfill(guild, make_config(), since, T0 - timedelta(days=30), errors))
    assert errors == []
    assert sorted(m.id for m in out) == ["2", "3", "4", "5", "6"]
    assert help_.history_calls == [old.created_at]
    assert general.history_calls == [T0 - timedelta(days=30)]
    assert [m.parent_channel_id for m in out if m.id == "6"] == ["400"]


def test_backfill_honours_channel_ids():
    guild, help_, general, _ = guild_with_everything()
    out = asyncio.run(backfill(guild, make_config(channel_ids=("100",)), {}, T0 - timedelta(days=1), []))
    assert sorted(m.id for m in out) == ["1", "2", "3", "4"]
    assert general.history_calls == []


def test_backfill_continues_past_unreadable_channel():
    secret = FakeChannel(150, "secret", fail=Forbidden("Missing Access"))
    help_ = FakeChannel(100, "help")
    post(help_, 1, "readable")
    guild = NS(id=900, name="Acme", text_channels=[secret, help_], forums=[])
    errors = []
    out = asyncio.run(backfill(guild, make_config(), {}, T0 - timedelta(days=1), errors))
    assert [m.id for m in out] == ["1"]
    assert errors == ["#secret: Forbidden: Missing Access"]


def test_bot_source_fetch_passes_last_seen_and_default_window():
    conn = connect(":memory:")
    upsert_messages(conn, [msg("a", minutes=0), msg("b", minutes=5)], frozenset())
    seen = {}

    def runner(token, config, since, default_since, errors):
        seen.update(token=token, since=since, default_since=default_since)
        errors.append("#secret: Forbidden: Missing Access")
        return [message_from_discord(post(FakeChannel(100, "help"), 9, "live", minutes=20), "900")]

    source = BotSource(conn, make_config(), token="tok", runner=runner, now=lambda: T0)
    stats, errors = ingest(conn, make_config(), source)
    assert seen["token"] == "tok" and seen["default_since"] == T0 - timedelta(days=30)
    assert seen["since"] == {"100": T0 + timedelta(minutes=5)}
    assert stats.inserted == 1 and errors == ["#secret: Forbidden: Missing Access"]
    assert conn.execute("SELECT source FROM messages WHERE id = '9'").fetchone()["source"] == "bot"
    assert last_seen(conn)["100"] == T0 + timedelta(minutes=20)


def test_invite_url_requests_read_only_permissions():
    assert BOT_PERMISSIONS == 66560
    assert invite_url("123") == "https://discord.com/oauth2/authorize?client_id=123&scope=bot&permissions=66560"
