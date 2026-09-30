# Triage benchmark: LLM only vs Jev + LLM

Date: 2026-09-30. Measured on a 141-message synthetic DiscordChatExporter export of a fictional developer-tool community (launch week of a v2.0 release: help channel with a thread, general chat, feedback, announcements). The export is deliberately pain-heavy (49% of messages are negative, a bug, or need a reply), and the answer key was written by hand, so treat these as directional, not a guarantee on real server data.

## Setup

| Path | Model | How it was run |
|---|---|---|
| LLM | OpenAI `gpt-5.4-mini` (triage prompt v2) | Discord Pulse triage agent, 6 batched calls of up to 25 messages |
| Jev | TypeSafe `jev-1.13-20260917` via OpenRouter | One request per message, 3 closed-set questions (needs_reply yes/no, kind, sentiment), 8 in parallel |

Prices: gpt-5.4-mini $0.75 / 1M input, $4.50 / 1M output (OpenAI pricing page, 2026-09-30). Jev cost measured from the OpenRouter key usage counter before and after a 141-request run.

## Accuracy

Answer key adjusted for the team's policy that bug reports anywhere need a reply.

| Metric | gpt-5.4-mini (triage v1) | gpt-5.4-mini (triage v2) | Jev |
|---|---|---|---|
| needs_reply recall | 79% | 100% | 100% (threshold 0.7) |
| needs_reply false positives | 9 community questions flagged | 0 | 1 ("docs search is really bad") |
| needs_reply agreement with gpt v2 | | | 98% |
| needs_reply ranking AUC (vs gpt v2) | | | 1.00 |
| kind accuracy | 80% | 89% | 84% |
| sentiment exact / within ±1 | 82% / 100% | 79% / 100% | 77% / 100% |

Jev's needs_reply probabilities were well spread (0.05 to 0.96) and usable for ranking. Its main errors were on staff messages (answers labelled as bug or question), which code can override, and bug-vs-docs ambiguity.

## Speed and cost

| | LLM only (gpt-5.4-mini) | Jev only | Jev + LLM hybrid |
|---|---|---|---|
| Wall time, 141 messages | ~11 s | 3.5 s (median 178 ms per request) | |
| Measured cost, 141 messages | $0.0427 | $0.0055 | |
| Cost per 1,000 messages | **$0.303** | **$0.039** (7.8x cheaper) | **$0.114** at 25% sent to the LLM (2.6x cheaper); $0.187 at this sample's 49% (1.6x) |

Hybrid = Jev labels every message (needs_reply, kind, sentiment); the LLM only writes topics for messages that matter (negative, needs a reply, or a bug). The hybrid estimate assumes LLM cost scales linearly with the share of messages sent to it; a topics-only prompt is shorter, so real hybrid cost should be at or below these numbers.

### Monthly projection

| Messages per day | LLM only | Jev + LLM (25%) | Jev only |
|---|---|---|---|
| 1,000 | $9.08 | $3.43 | $1.16 |
| 5,000 | $45.39 | $17.16 | $5.81 |
| 20,000 | $181.54 | $68.62 | $23.24 |

## Takeaways

- Jev matches the LLM on the decision that drives the mod queue (needs_reply) at about an eighth of the cost and a third of the latency, and its probability is a good priority score.
- The LLM stays necessary for anything generative (topics, theme names, digests, investigations), so the realistic saving from adding Jev is about 2 to 3x, not 8x.
- Model choice is a lever of similar size: OpenAI lists `gpt-6-luna` at $0.10 / $0.50 per 1M tokens, about 7x below gpt-5.4-mini. Its triage accuracy has not been measured yet.
- Re-run this benchmark on real exported data (via the read-only bot) before relying on these numbers; the Plan 4 eval harness automates it and gates Jev on at least 90% agreement.
