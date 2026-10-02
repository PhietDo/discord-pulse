"""discord.py glue for the read-only bot. The only module that imports discord."""
from __future__ import annotations

from datetime import datetime

from pulse.config import Config
from pulse.models import Message
from pulse.sources.bot_source import backfill


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
