"""Discord jump links. For a message in a thread, channel_id is the thread id."""


def jump_link(guild_id: str, channel_id: str, message_id: str) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
