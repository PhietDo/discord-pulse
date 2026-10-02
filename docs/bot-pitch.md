# Discord Pulse: a read-only bot for the community team

## What it is

Discord Pulse helps the community team see how people feel about the product and where they get stuck. It reads messages in the channels you allow and produces three things for the team: a sentiment trend, a ranked list of recurring pain points (bugs, confusing docs, setup trouble), and a queue of questions that have not had a staff reply.

## What the bot does and does not do

- It reads messages in the channels it can see.
- It never posts, replies, reacts, sends DMs, edits, deletes, pins, or changes any setting. The invite asks only for read access and the code never calls a write action. A bot also gets the server's @everyone permissions, so to have Discord enforce read-only, deny Send Messages and Add Reactions to the bot's role.
- It does not read private channels or private threads unless you give it access to them.

## Exactly what access it asks for

- Scope: `bot`.
- Permissions: **View Channels** and **Read Message History**. Nothing else: no Administrator, no Send Messages, no Manage permissions.
- Privileged intent: **Message Content Intent**, which Discord requires for a bot to read message text.

You decide which channels it reads through normal channel permissions. The team can narrow it further in its own settings.

## Where the data goes

- Messages are stored in a database file on the community lead's computer. Nothing is hosted on a server.
- To label each message, its text is sent to the AI provider the team has configured: Anthropic, OpenAI, or OpenRouter (which forwards it to the chosen model's provider). Anthropic's and OpenAI's APIs do not train on API data by default; for OpenRouter, the chosen provider's policy applies.
- If the team turns on the Jev classifier, each message's text is also sent to Jev (through OpenRouter) for a first-pass label.
- When the community lead chooses to send a pain point to GitHub or Linear, the issue includes short message excerpts and links to the messages, never author names. If the repo is public, so are those excerpts.
- If Slack alerts are turned on, an alert includes a short excerpt and, for unanswered frustrated messages, the author's display name, posted to the team's Slack.
- The dashboard runs on that computer only and is not reachable from the internet.

## How to remove it

Kick the bot from the server. That ends all access immediately. On request, the community lead deletes the local database.

## Setup (about five minutes)

1. Whoever owns the bot application creates it in the Discord Developer Portal (Applications, New Application, Bot) and turns on **Message Content Intent** on the Bot page.
2. They run `python -m pulse.run bot-invite --client-id <Application ID>` and send you the link it prints. The link asks only for View Channels and Read Message History.
3. A member with Manage Server opens the link and picks this server.
4. Optional: limit which channels it can see using channel permissions.

Questions or concerns: ask the community lead before or after it is added; it can be removed at any time.
