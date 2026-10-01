"""Wires sources, agents, and rules into runnable stages. Plain code, no LLM decisions."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from pulse.agents.base import Backend
from pulse.agents.digest import DigestResult
from pulse.agents.investigate import InvestigationResult
from pulse.agents.llm import LLMClient
from pulse.agents.theme import ThemeStats, run_themes
from pulse.agents.triage import TriageStats, run_triage
from pulse.citations import render_text
from pulse.config import Config
from pulse.links import jump_link
from pulse.models import from_iso
from pulse.modqueue import ModQueueStats, refresh_mod_queue
from pulse.sources.base import Source
from pulse.sources.file_source import FileSource
from pulse.store import UpsertStats, upsert_messages, sync_launches


def build_backends(config: Config) -> dict[str, Backend]:
    # Imported here so a missing SDK only matters for providers actually in use.
    providers = {ref.provider for ref in config.models.values()}
    backends: dict[str, Backend] = {}
    if "anthropic" in providers:
        from pulse.agents.providers.anthropic_backend import AnthropicBackend

        backends["anthropic"] = AnthropicBackend()
    if "openai" in providers or "openrouter" in providers:
        from pulse.agents.providers.openai_backend import OpenAIBackend

        if "openai" in providers:
            backends["openai"] = OpenAIBackend()
        if "openrouter" in providers:
            backends["openrouter"] = OpenAIBackend(openrouter=True)
    return backends


def build_llm(conn: sqlite3.Connection, config: Config) -> LLMClient:
    classifier = None
    if config.classifier is not None and config.classifier.enabled:
        from pulse.agents.providers.jev_backend import JevBackend

        classifier = JevBackend()
    return LLMClient(conn, config, build_backends(config), classifier=classifier)


def ingest(conn: sqlite3.Connection, config: Config, source: Source) -> tuple[UpsertStats, list[str]]:
    messages = source.fetch(None)
    if config.channel_ids:
        ids = set(config.channel_ids)
        messages = (
            m for m in messages if m.channel_id in ids or m.parent_channel_id in ids
        )
    stats = upsert_messages(conn, messages, config.team_member_ids)
    return stats, list(source.errors)


@dataclass
class PipelineReport:
    ingest: UpsertStats
    ingest_errors: list[str]
    triage: TriageStats
    modqueue: ModQueueStats
    themes: ThemeStats | None = None


def run_pipeline(
    conn: sqlite3.Connection,
    config: Config,
    llm: LLMClient,
    *,
    source: Source | None = None,
    now: datetime | None = None,
) -> PipelineReport:
    source = source if source is not None else FileSource(config.imports_dir)
    now = now if now is not None else datetime.now(timezone.utc)
    ingest_stats, errors = ingest(conn, config, source)
    sync_launches(conn, config.launches)
    triage_stats = run_triage(conn, llm)
    theme_stats = run_themes(conn, llm, now)
    queue_stats = refresh_mod_queue(conn, config, now)
    return PipelineReport(ingest_stats, errors, triage_stats, queue_stats, themes=theme_stats)


def format_ingest(stats: UpsertStats, errors: list[str]) -> str:
    lines = [f"ingest: inserted {stats.inserted}, updated {stats.updated}, unchanged {stats.unchanged}"]
    lines += [f"  skipped: {e}" for e in errors]
    return "\n".join(lines)


def format_triage(stats: TriageStats) -> str:
    line = f"triage: triaged {stats.triaged}, failed batches {stats.failed_batches}"
    if stats.jev_labeled or stats.escalated or stats.classifier_failed or stats.kept_llm:
        line += (
            f"\n  jev: labeled {stats.jev_labeled}, escalated to LLM {stats.escalated},"
            f" classifier failures {stats.classifier_failed}"
        )
        if stats.kept_llm:
            line += f", kept existing LLM labels {stats.kept_llm}"
    if stats.left_untriaged:
        line += f"\n  daily budget cap reached: {stats.left_untriaged} messages left untriaged for the next run"
    elif stats.skipped_budget_batches:
        line += f"\n  daily budget cap reached: {stats.skipped_budget_batches} batches skipped"
    return line


def format_modqueue(stats: ModQueueStats) -> str:
    return f"mod queue: opened {stats.opened}, updated {stats.updated}, auto-closed {stats.auto_closed}"


def format_themes(stats: ThemeStats) -> str:
    jev_failed = f", jev failed {stats.jev_failed}" if stats.jev_failed else ""
    lines = [
        f"themes: considered {stats.considered}, jev assigned {stats.jev_assigned}{jev_failed},"
        f" llm batches {stats.llm_batches} (failed {stats.failed_batches}), new themes {stats.created},"
        f" assignments {stats.assigned}, merges {stats.merged}, renames {stats.renamed}"
    ]
    lines += [f"  rejected: {r}" for r in stats.rejected]
    if stats.skipped_budget:
        lines.append("  daily budget cap reached: remaining messages will be themed on the next run")
    return "\n".join(lines)


def _removed_note(removed: list[str]) -> str:
    return f"\n\n(removed {len(removed)} citation(s) to messages the agent was not shown)" if removed else ""


def format_digest(result: DigestResult, conn: sqlite3.Connection) -> str:
    header = f"digest #{result.digest_id} ({result.kind}, {result.period_start[:10]} to {result.period_end[:10]})"
    return f"{header}\n\n{render_text(result.markdown, conn)}{_removed_note(result.removed_citations)}"


def format_investigation(result: InvestigationResult, conn: sqlite3.Connection) -> str:
    header = f"investigation #{result.investigation_id} ({result.tool_calls} tool calls)"
    return f"{header}\n\n{render_text(result.markdown, conn)}{_removed_note(result.removed_citations)}"


def format_report(report: PipelineReport) -> str:
    lines = [
        format_ingest(report.ingest, report.ingest_errors),
        format_triage(report.triage),
    ]
    if report.themes is not None:
        lines.append(format_themes(report.themes))
    lines.append(format_modqueue(report.modqueue))
    return "\n".join(lines)


def format_queue(items, now: datetime) -> str:
    if not items:
        return "mod queue: nothing open"
    lines = [f"mod queue: {len(items)} open, highest priority first"]
    for i, it in enumerate(items, start=1):
        age_h = (now - from_iso(it["created_at"])).total_seconds() / 3600
        p = it["needs_reply_p"]
        priority = f" p={p:.2f}" if p is not None else ""
        text = " ".join(it["content"].split())
        lines.append(
            f"{i:>2}. [{it['reason']}{priority}] {it['author_name']} in #{it['channel_name']},"
            f" {age_h:.0f}h ago: {text[:90]}"
        )
        lines.append(f"    {jump_link(it['guild_id'], it['channel_id'], it['message_id'])}")
    return "\n".join(lines)
