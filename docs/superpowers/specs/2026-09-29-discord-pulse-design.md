# Discord Pulse: Design Spec

Date: 2026-09-29
Status: Draft, awaiting review

## 1. Purpose

An internal, single-tenant tool for a DevRel / community lead who follows one product's Discord server. It answers three questions:

1. Is a launch, feature, or release landing well? (sentiment over time and around launch dates)
2. What recurring pain points are users hitting? (bugs, docs confusion, setup friction, missing features), with evidence messages
3. Who needs a human reply right now? (mod queue of frustrated users, unanswered questions, escalating threads)

Success: the lead opens the dashboard weekly or right after a launch and within a few minutes can say "sentiment on X dipped, these three pain points drove it, here are the messages and who said them, and these five people still need a reply."

Analysis is performed at runtime by Claude subagents. The implementation itself is built via subagent-driven development.

### Non-goals (v1)

Multi-server / multi-tenant, authentication, real-time websocket updates, per-user profiles or scoring, auto-replying in Discord, any write access to Discord.

### Assumptions

- The user is NOT a server admin and cannot add a bot today. File import is the primary ingest path. The read-only bot adapter is built so the user can pitch it to the mod team (section 13); once approved, it becomes the primary path and file import remains for backfill.
- How export files are produced is outside this tool. The tool never reads Discord with a user account token. Exporters that run on a user token are self-botting under Discord's Terms of Service and risk the account; the README states this plainly.
- Volume: a few hundred to a few thousand messages per day.
- Runs locally on one machine (macOS, launchd scheduling).

## 2. Stack and layout

- Python 3.12, FastAPI, Jinja2 templates + htmx, Chart.js (from cdn.jsdelivr.net), SQLite (stdlib `sqlite3`), `anthropic` and `openai` SDKs, `discord.py` (bot adapter only), pytest.
- Project root: `~/discord-pulse`, package `pulse/`.
- Single config file `pulse.toml`:

```toml
[server]
guild_id = "123"
channel_ids = ["456", "789"]        # empty = all readable channels
team_member_ids = ["111", "222"]    # staff; replies from these count as "answered"

[mod_queue]
reply_window_hours = 12
frustration_threshold = -2          # sentiment <= this enters the queue regardless

[models]                            # "provider:model"; provider = anthropic | openai | openrouter
triage = "anthropic:claude-haiku-4-5-20251001"
theme = "anthropic:claude-sonnet-5"
digest = "anthropic:claude-opus-5-5"
investigate = "openrouter:anthropic/claude-sonnet-5"

[budget]
daily_usd_cap = 5.00

# USD per million tokens. Required for every model in [models], except
# openrouter models, where the cost OpenRouter reports per response is used
# when present and this table is the fallback.
[pricing."anthropic:claude-haiku-4-5-20251001"]
input = 1.00
output = 5.00
cache_read = 0.10

[[launches]]
name = "v2.0 SDK"
date = "2026-09-15"
keywords = ["v2", "new sdk", "migration"]
```

Secrets come from environment variables, never the config file: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY` (only the ones for providers in use are required), and `DISCORD_BOT_TOKEN` (bot adapter only). Config validation fails at startup, naming the missing variable, if a configured provider has no key or a non-OpenRouter model has no pricing entry, so the budget cap can never be silently bypassed.

The pricing values shown are placeholders; the user fills in current prices.

### Module layout

```
pulse/
  config.py          # load + validate pulse.toml
  db.py              # schema, connection, migrations
  models.py          # dataclasses: Message, TriageResult, Theme, ...
  links.py           # Discord jump-link construction
  sources/
    base.py          # Source protocol
    file_source.py   # DiscordChatExporter JSON + simple CSV
    bot_source.py    # discord.py read-only backfill + stream
  agents/
    llm.py           # provider-neutral client: structured output, tool calls, retries, cost logging, budget gate
    providers/
      anthropic_backend.py
      openai_backend.py   # also serves OpenRouter via base_url
    pricing.py
    triage.py
    theme.py
    digest.py
    investigate.py   # tool-using agent, read-only tools
    prompts/         # versioned prompt files
  pipeline.py        # orchestrator: ingest -> triage -> theme -> mod queue (plain code)
  modqueue.py        # deterministic queue rules
  stats.py           # aggregate queries used by dashboard + digest
  web/
    app.py           # FastAPI routes
    templates/
    static/
  run.py             # `python -m pulse.run` entrypoint
  eval.py            # `python -m pulse.eval`
tests/
```

## 3. Ingest

One interface:

```python
class Source(Protocol):
    def fetch(self, since: datetime | None) -> Iterator[Message]: ...
```

- **FileSource**: reads files from an `imports/` folder. Supports DiscordChatExporter JSON (which carries guild id, channel id, message id, author id/name/avatar, timestamps, reply references, thread info) and a simple CSV with required columns `guild_id,channel_id,message_id,author_id,author_name,content,created_at` and optional `reply_to_id,thread_id`. Unknown formats are rejected with a clear error naming the file.
- **BotSource**: read-only bot with the Message Content intent and only View Channel + Read Message History permissions. On start, backfills history for configured channels and their threads since the last stored message, then streams new messages.
- Upsert keyed on Discord `message_id`: re-importing is idempotent. Edited messages update content; the triage row is invalidated for re-processing.
- `is_team` is computed at ingest from `team_member_ids`.

## 4. Data model (SQLite)

| Table | Columns (key ones) |
|---|---|
| `messages` | `id` (Discord message id, PK), `guild_id`, `channel_id` (the containing channel or thread id), `channel_name`, `thread_id` (nullable), `author_id`, `author_name`, `author_avatar_url`, `is_team`, `content`, `created_at`, `edited_at`, `reply_to_id`, `source` |
| `triage` | `message_id` (PK/FK), `sentiment` (int -2..2), `confidence` (0-1), `kind` (bug, question, feature_request, docs, praise, other), `topics` (JSON list), `needs_reply` (bool), `prompt_version`, `run_id`, `created_at` |
| `themes` | `id`, `name`, `description`, `status` (active, merged), `merged_into` (nullable FK), `created_at` |
| `message_themes` | `message_id`, `theme_id` |
| `theme_events` | `id`, `kind` (create, rename, merge), `payload` (JSON), `run_id`, `created_at` |
| `launches` | `id`, `name`, `date`, `keywords` (synced from config) |
| `mod_queue` | `id`, `message_id`, `thread_id`, `reason` (unanswered, frustrated), `status` (open, handled, dismissed), `opened_at`, `closed_at` |
| `digests` | `id`, `kind` (weekly, launch), `period_start`, `period_end`, `launch_id` (nullable), `markdown`, `cited_message_ids` (JSON), `run_id`, `created_at` |
| `investigations` | `id`, `question`, `context` (JSON: the dashboard object it was launched from), `markdown`, `cited_message_ids`, `run_id`, `created_at` |
| `agent_runs` | `id`, `agent`, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cost_usd`, `status` (ok, failed, skipped_budget), `error`, `started_at`, `finished_at` |

Theme history is never rewritten: merges set `status=merged, merged_into=X`; queries resolve merged themes to their target at read time.

## 5. Message attribution and links

Every message the dashboard shows (evidence quotes, mod queue items, digest and investigation citations, search results) is rendered with one shared component, `message_card`, containing:

- author avatar, author display name, and a "team" badge when `is_team`
- channel/thread name and timestamp
- the message text (truncated with expand)
- sentiment and kind chips
- **"Open in Discord" jump link**: `https://discord.com/channels/{guild_id}/{channel_id}/{message_id}`, built by `pulse/links.py`. For a message in a thread, `channel_id` is the thread id, which Discord resolves correctly.
- a link to the author's filtered view: `/messages?author_id=...` listing everything that person said in the selected window, with the same cards

Agent-written reports (digest, investigate) cite messages as `[[msg:<message_id>]]` tokens in their markdown. The renderer replaces each token with an inline citation showing the author name, linked to the Discord jump link, plus a hover card. Tokens that reference an unknown message id are rendered as "[missing message]" and logged, never silently dropped. Agents are instructed to cite only ids present in their input, and output validation strips and logs any id not in the input set.

## 6. Agents

All agents go through `agents/llm.py`, which provides: JSON-schema-validated structured output, one retry on validation failure, token + cost logging to `agent_runs`, prompt caching for static prompt prefixes, and a budget gate that refuses calls once today's `cost_usd` sum reaches `daily_usd_cap` (recorded as `skipped_budget`). Each agent call is an independent subagent with its own context; no agent sees another agent's conversation, only persisted DB outputs.

### 6.0 Providers

Agents never import a provider SDK. They call `llm.complete(agent_name, system, messages, schema)` or `llm.run_tools(agent_name, system, messages, tools, schema, max_calls)`; `llm.py` resolves the agent's `provider:model` from config and dispatches to a backend implementing one protocol:

```python
class Backend(Protocol):
    def complete(self, model, system, messages, schema, cache_prefix) -> BackendResult: ...
    def tool_step(self, model, system, messages, tools) -> BackendStep: ...
# BackendResult carries: parsed JSON, input/output/cache-read tokens, reported cost (optional)
```

| Concern | anthropic | openai | openrouter |
|---|---|---|---|
| SDK | `anthropic` | `openai` | `openai` with `base_url=https://openrouter.ai/api/v1` |
| Structured output | forced single tool whose input schema is the output schema | `response_format` json_schema (strict) | json_schema when the model supports it, else JSON mode + local validation |
| Tool calling (Investigate) | native tool use | function calling | function calling |
| Prompt caching | `cache_control` on the static prefix | automatic prefix caching | passes through provider caching |
| Cost | tokens x pricing table | tokens x pricing table | reported `usage.cost` when present, else pricing table |

Tool and schema definitions are written once in a neutral format and translated per backend. Local JSON-schema validation runs on every backend's output regardless of provider guarantees.

Model names in 6.1-6.4 are the defaults; any agent can be pointed at any provider:model.

### 6.1 Triage (Haiku 4.5, parallel)

- Input: batches of ~25 untriaged messages. Each message is sent with context: its reply-to parent and up to 3 preceding messages in the same channel/thread.
- Static prefix (cached): instructions, label definitions, and few-shot examples from this community's labeled set (dev-community tone: sarcasm, "this is sick", "lol broke again").
- Output per message: `sentiment`, `confidence`, `kind`, `topics` (1-3 short noun phrases), `needs_reply`.
- Batches run concurrently (bounded, default 4 in flight).
- Skips messages that are empty or bot-authored.

### 6.2 Theme (Sonnet 5, daily)

- Input: topic tags + kinds from newly triaged messages (with a sample of message ids per tag) and the current active theme list.
- Output: assignments of messages to existing themes, proposed new themes (name + description), and proposed merges/renames.
- Code applies proposals and logs each to `theme_events`. Guardrails: max 5 new themes and 3 merges per run; a merge requires both themes to exist and be active.

### 6.3 Digest (Opus 5.5, weekly + on demand per launch)

- Input: precomputed stats from `stats.py` (sentiment trend, top themes by score, deltas vs prior period, mod queue counts) plus up to 60 representative messages (highest-impact per theme, with ids and authors).
- Output: markdown with sections "What's landing well", "Top pain points" (trend + cited quotes), "Needs attention", "Suggested priorities". Must cite with `[[msg:id]]`.
- Launch digest compares the 14 days before vs after a launch date and focuses on themes/messages matching launch keywords.

### 6.4 Investigate (Sonnet 5, on demand)

- Launched from a dashboard button on a sentiment dip, theme, or launch, with that object as context and an optional free-text question.
- Tools (read-only, parameterized, no raw SQL): `query_stats(metric, start, end, theme_id?, channel_id?)`, `search_messages(text?, theme_id?, author_id?, start?, end?, limit)`, `get_thread(message_id)`.
- Max 12 tool calls per investigation. Output: short markdown explanation with `[[msg:id]]` citations, saved to `investigations`.
- Runs as a background task; the page polls via htmx until done.

### Pain point score

`score = volume_7d * mean_negativity * (1 + max(0, trend))`, where `mean_negativity = mean(max(0, -sentiment))` for messages in the theme, and `trend = (volume_7d - volume_prev_7d) / max(volume_prev_7d, 1)`.

## 7. Mod queue rules (plain code, `modqueue.py`)

- **unanswered**: triage `needs_reply = true`, no reply from an `is_team` author in the same thread (or replying to it) within `reply_window_hours`.
- **frustrated**: sentiment <= `frustration_threshold`, regardless of replies.
- Auto-close: an unanswered item closes as handled when a team reply appears later.
- One open item per thread; new triggers in the same thread update the existing item.

## 8. Dashboard views

Time window selector (7d/30d/90d/custom) on every page.

1. **Overview**: sentiment trend line + message volume bars with launch markers; top 5 rising pain points; open mod queue count; latest digest excerpt.
2. **Pain points**: themes ranked by score, each with a sparkline, kind breakdown, and evidence message cards (section 5); Investigate button.
3. **Launch**: select a launch; before/after sentiment and volume; themes and message cards matching keywords; "Generate launch digest" button.
4. **Bugs**: every triaged message with kind = bug, grouped by theme, newest first, with message cards (author + jump link), counts per theme over the window, and whether each has a staff reply. Bug reports also enter the mod queue under the normal needs_reply rules, including bug reports posted in general chat channels (user decision 2026-09-30).
5. **Mod queue**: open items oldest first with reason badge and message card (author + jump link); Mark handled / Dismiss buttons (htmx).
6. **Messages**: searchable/filterable list of message cards (text, author, channel, theme, kind, sentiment). This is the target of author links.
7. **Reports**: digests and investigations, rendered markdown with resolved citations.
8. **Runs**: agent run history, daily cost vs cap, failures with error text.

## 9. Orchestration

`python -m pulse.run` with subcommands:

- `ingest [--source file|bot]`
- `triage`
- `themes`
- `modqueue`
- `digest [--launch NAME]`
- `pipeline` (ingest -> triage -> themes -> modqueue; the default launchd job, every 30 min)
- `bot` (long-running BotSource streamer)
- `web` (serves the dashboard on localhost)
- `seed-demo` (writes a synthetic community dataset, with pre-baked triage, themes, mod queue items and a digest, into a separate `demo.db`; `web --db demo.db` serves it; costs nothing and touches no real data)

Weekly digest is a separate launchd job. Each stage is idempotent and picks up only unprocessed work.

## 10. Error handling

- Schema-invalid agent output: retry once, then mark the batch failed in `agent_runs`; no partial writes (each batch's writes are one transaction).
- Budget cap reached: stages stop issuing calls, runs recorded as `skipped_budget`, dashboard shows a banner.
- API errors / rate limits: exponential backoff (3 attempts) inside `llm.py`, then fail the batch.
- Bot disconnects: discord.py reconnect; on restart, backfill from the last stored message.
- Bad import file: skipped with an error naming the file and line; other files proceed.
- Re-triage: bumping `prompt_version` and running `triage --since DATE --force` reprocesses a range.

## 11. Testing

- pytest throughout; TDD per task.
- `FakeLLM` implementing the same interface as `llm.py`, returning canned structured outputs, so pipeline, theme application, mod queue, and citation rendering are tested deterministically with no API cost.
- Adapter tests against small recorded fixture files (DiscordChatExporter JSON, CSV). BotSource tested at the message-mapping layer with fake discord.py objects.
- `links.py` and citation rendering covered by unit tests including thread messages and unknown ids.
- Web routes tested with FastAPI TestClient against a seeded SQLite DB.
- Eval harness `python -m pulse.eval [--model provider:model ...]`: runs triage against `eval/gold.jsonl` (~200 hand-labeled messages) and reports sentiment accuracy, kind macro-F1, needs_reply recall, and cost, one row per model so providers can be compared side by side. Run before merging any prompt change or switching a provider. The seed gold set is created by the user; a tiny synthetic set ships for tests.
- Each backend has contract tests against recorded provider responses (no network), covering structured output, tool calls, cache token accounting, and OpenRouter reported cost.

## 12. Build process

Spec -> implementation plan (writing-plans) -> subagent-driven development: each plan task implemented by a fresh subagent, with review between tasks.

Build order favors what the user can use without admin rights: file ingest, agents, dashboard and demo mode first; the bot adapter last.

## 13. Bot setup and mod-team pitch

The repo ships `docs/bot-pitch.md`, a one-page document the user can hand to the mod team, and the `seed-demo` dashboard to show alongside it. It covers:

- What the bot does: reads messages in the listed channels to produce sentiment, pain-point and needs-reply reports for the community team. It never posts, reacts, DMs, or modifies anything.
- Exact permissions requested: `bot` scope with View Channels and Read Message History only. Message Content Intent enabled (needed to read text). No Administrator, no Send Messages, no Manage permissions.
- Where data lives: on the user's machine in SQLite; message text is sent to the configured LLM provider for analysis. Mods choose which channels it may read via channel permissions; private channels and private threads are excluded unless they grant access.
- How to remove it: kick the bot; the user deletes the local DB on request.

Setup steps (performed by whoever owns the application and a server admin):

1. Discord Developer Portal: create an Application, add a Bot, generate the token (stored as `DISCORD_BOT_TOKEN`).
2. Enable Message Content Intent on the Bot page.
3. OAuth2 URL Generator: scope `bot`, permissions View Channels + Read Message History.
4. A member with Manage Server opens the URL and selects the server.
5. Channel access is then governed by channel permissions; `channel_ids` in `pulse.toml` narrows which readable channels are analyzed. Private threads are out of scope for v1.

## 14. Addendum (2026-09-30): Jev first-pass classifier

Evidence: docs/benchmarks/2026-09-30-triage-llm-vs-jev.md (Jev matched gpt-5.4-mini on needs_reply at ~1/8 the cost). Implemented in Plan 2.

### 14.1 What Jev does and doesn't do

Jev (TypeSafe `jev-latest`, called through OpenRouter's `/api/v1/systemone`) answers closed-set questions only. It is used for:

- **Triage labels** on every message: `needs_reply` (Noul, a probability), `kind` (Choice over KINDS), `sentiment` (Choice over -2..2), each with confidence.
- **Mod queue priority**: open items are ordered by `needs_reply_p` descending, then age (replaces "oldest first" in section 8).
- **Theme assignment** (Plan 2 theme agent): a Choice over existing active themes plus `none`, per message.

The LLM keeps everything generative: topics, new theme names and descriptions, merges, digests, investigations.

### 14.2 Triage flow with the classifier enabled

1. **Stage A (Jev)**: for each untriaged, non-bot message, one request with the same per-message state the LLM triage sends (content, author, is_team, channel, reply_to, context) and three questions. Code then overrides staff messages (`is_team`): sentiment 0, kind `other`, needs_reply false.
2. **Stage B (LLM)**: messages are escalated to the existing LLM triage (same prompt and schema) when any of: sentiment < 0, `needs_reply_p` >= `needs_reply_threshold`, kind in `escalate_kinds`, or a Jev confidence below `min_confidence`. The LLM result replaces Jev's labels for those messages and supplies topics.
3. Non-escalated messages keep Jev's labels with `topics = []`.
4. A Jev failure for a message (after the usual retries) escalates that message to Stage B; it never blocks triage.

With the classifier disabled, triage is exactly the Plan 1 LLM-only path.

### 14.3 Configuration

```toml
[classifier]
enabled = true
model = "jev:jev-latest"              # provider "jev" uses OPENROUTER_API_KEY
needs_reply_threshold = 0.7
min_confidence = 0.6
escalate_kinds = ["bug", "docs", "feature_request", "praise"]

[pricing."jev:jev-latest"]
per_request = 0.0000387               # measured 2026-09-30; USD per request
```

`per_request` pricing is a new pricing form for request-priced models; config validation requires it for the `jev` provider when the classifier is enabled.

### 14.4 Data model changes

`triage` gains `needs_reply_p REAL` (nullable; null for LLM-only rows), `kind_confidence REAL`, and `labeler TEXT` (`jev` or `llm`). Jev calls are logged in `agent_runs` with agent `classifier` and count toward the daily budget.

### 14.5 Gate

The Plan 4 eval harness scores Jev against `eval/gold.jsonl` alongside the LLM. If Jev's needs_reply agreement with the gold set is below 90%, the eval output says so and recommends `enabled = false`. The switch is manual; nothing auto-disables in v1.

### 14.6 Testing

A `FakeClassifier` mirrors `FakeBackend`. Tests cover staff overrides, each escalation rule, Jev failure escalation, queue ordering by `needs_reply_p`, and budget accounting for per-request pricing. The Jev backend gets contract tests against recorded `systemone` responses (no network).
