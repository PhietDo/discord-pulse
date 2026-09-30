# Discord Pulse

Internal DevRel tool: how a product's Discord community feels, which pain points recur, and who still needs a reply. Analysis is done by LLM subagents (Anthropic, OpenAI, or OpenRouter, chosen per agent).

## Setup

```bash
uv venv --python 3.12 .venv
uv pip install -e '.[dev]'
cp pulse.toml.example pulse.toml   # then edit
export ANTHROPIC_API_KEY=...       # and/or OPENAI_API_KEY, OPENROUTER_API_KEY
```

## Getting messages in

Drop export files into `imports/`:

- DiscordChatExporter JSON (one file per channel or thread), or
- CSV with columns `guild_id,channel_id,message_id,author_id,author_name,content,created_at` and optional `reply_to_id,thread_id,channel_name,author_avatar_url,is_bot,edited_at`.

Re-importing is safe: messages are keyed on Discord's message id, and edited messages are re-triaged.

**Terms of Service warning:** this tool never reads Discord with a user account token. Exporters that run on your user token count as self-botting under Discord's Terms of Service and can get the account banned. The supported live route is a read-only bot added by a server admin (coming in a later release).

## Commands

```bash
.venv/bin/python -m pulse.run ingest              # import files from imports/
.venv/bin/python -m pulse.run triage              # label new messages
.venv/bin/python -m pulse.run triage --since 2026-09-01 --force   # re-label a range
.venv/bin/python -m pulse.run modqueue            # refresh who needs a reply
.venv/bin/python -m pulse.run pipeline            # all of the above
```

Spending is capped by `[budget] daily_usd_cap`; once reached, agent calls stop for the day and the run says so.

## Tests

```bash
.venv/bin/pytest
```
