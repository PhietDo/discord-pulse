"""discord.py glue for the read-only bot. The only module that imports discord."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from pulse.config import Config
from pulse.models import Message
from pulse.sources.bot_source import FLUSH_SECONDS, BotStreamer, backfill


def intents():
    import discord

    i = discord.Intents.none()
    i.guilds = True
    i.guild_messages = True
    i.message_content = True
    return i


def run_client(client, token: str, errors: list[str]) -> None:
    import discord

    try:
        client.run(token, log_handler=None)
    except discord.LoginFailure:
        errors.append("Discord rejected DISCORD_BOT_TOKEN")
    except discord.PrivilegedIntentsRequired:
        errors.append("turn on Message Content Intent on the bot's page in the Discord Developer Portal")


def run_backfill(token: str, config: Config, since: dict, default_since: datetime, errors: list[str]) -> list[Message]:
    import discord

    client = discord.Client(intents=intents())
    out: list[Message] = []

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(int(config.guild_id))
            if guild is None:
                errors.append(f"the bot is not in server {config.guild_id}; see python -m pulse.run bot-invite")
            else:
                out.extend(await backfill(guild, config, since, default_since, errors))
        finally:
            await client.close()

    run_client(client, token, errors)
    return out


def run_streamer(token: str, config: Config, conn, *, log=print) -> BotStreamer:
    """Connect, catch up on every (re)connect, then stream new and edited messages until stopped."""
    import discord

    client = discord.Client(intents=intents())
    streamer = BotStreamer(conn, config)
    errors: list[str] = []
    state = {"flusher": None}

    async def flush_forever():
        while True:
            await asyncio.sleep(FLUSH_SECONDS)
            try:
                await asyncio.to_thread(streamer.flush)
            except Exception as e:
                log(f"bot: flush failed, will retry: {type(e).__name__}: {e}")

    @client.event
    async def on_ready():
        guild = client.get_guild(int(config.guild_id))
        if guild is None:
            log(f"bot: not in server {config.guild_id}; see python -m pulse.run bot-invite")
            await client.close()
            return
        # on_ready fires again after a reconnect, so this also catches up on anything missed.
        default_since = datetime.now(timezone.utc) - timedelta(days=config.bot_backfill_days)
        since = await asyncio.to_thread(streamer.since)
        caught_up = await backfill(guild, config, since, default_since, errors)
        streamer.add_many(caught_up)
        for e in errors:
            log(f"bot: {e}")
        errors.clear()
        log(f"bot: connected to {guild.name}; caught up on {len(caught_up)} messages; streaming")
        if state["flusher"] is None:
            state["flusher"] = asyncio.create_task(flush_forever())

    @client.event
    async def on_message(message):
        streamer.add(message)

    @client.event
    async def on_message_edit(before, after):
        streamer.add(after)

    run_client(client, token, errors)
    for e in errors:
        log(f"bot: {e}")
    streamer.flush()
    return streamer
