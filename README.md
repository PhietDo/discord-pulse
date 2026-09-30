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
- CSV with columns `guild_id,channel_id,message_id,author_id,author_name,content,created_at` and optional `reply_to_id,thread_id,channel_name,author_avatar_url,is_bot,edited_at,parent_channel_id`.

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

`--since` dates are treated as UTC midnight. `--force` requires `--since` (re-triaging your whole history isn't allowed by accident).

Spending is capped by `[budget] daily_usd_cap`; once reached, agent calls stop for the day and the run says so. The cap resets at UTC midnight.

## Jev first pass (optional)

With `[classifier] enabled = true`, triage asks Jev (a cheap closed-set classifier, via OpenRouter) about every message first. Confident, low-stakes messages are labelled by Jev alone; anything negative, likely to need a reply, a bug, docs issue, feature request or praise, or low-confidence, still goes to the triage LLM for full labels and topics. Staff messages are always neutral. If Jev fails on a message, the LLM handles it.

Measured on synthetic data this cut triage cost by roughly 2-3x at the same needs-reply accuracy (see `docs/benchmarks/`). Re-check on your own data before relying on it.

List the mod queue, highest priority first, with links to each message:

```bash
.venv/bin/python -m pulse.run queue --limit 20
```

## Tests

```bash
.venv/bin/pytest
```
