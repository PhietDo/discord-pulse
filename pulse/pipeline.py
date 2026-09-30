"""Wires sources, agents, and rules into runnable stages. Plain code, no LLM decisions."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from pulse.agents.base import Backend
from pulse.agents.llm import LLMClient
from pulse.agents.triage import TriageStats, run_triage
from pulse.config import Config
from pulse.modqueue import ModQueueStats, refresh_mod_queue
from pulse.sources.base import Source
from pulse.sources.file_source import FileSource
from pulse.store import UpsertStats, upsert_messages


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
    return LLMClient(conn, config, build_backends(config))


def ingest(conn: sqlite3.Connection, config: Config, source: Source) -> tuple[UpsertStats, list[str]]:
    stats = upsert_messages(conn, source.fetch(None), config.team_member_ids)
    return stats, list(source.errors)


@dataclass
class PipelineReport:
    ingest: UpsertStats
    ingest_errors: list[str]
    triage: TriageStats
    modqueue: ModQueueStats


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
    triage_stats = run_triage(conn, llm)
    queue_stats = refresh_mod_queue(conn, config, now)
    return PipelineReport(ingest_stats, errors, triage_stats, queue_stats)


def format_ingest(stats: UpsertStats, errors: list[str]) -> str:
    lines = [f"ingest: inserted {stats.inserted}, updated {stats.updated}, unchanged {stats.unchanged}"]
    lines += [f"  skipped: {e}" for e in errors]
    return "\n".join(lines)


def format_triage(stats: TriageStats) -> str:
    line = f"triage: triaged {stats.triaged}, failed batches {stats.failed_batches}"
    if stats.skipped_budget_batches:
        line += f"\n  daily budget cap reached: {stats.skipped_budget_batches} batches skipped"
    return line


def format_modqueue(stats: ModQueueStats) -> str:
    return f"mod queue: opened {stats.opened}, updated {stats.updated}, auto-closed {stats.auto_closed}"


def format_report(report: PipelineReport) -> str:
    return "\n".join([
        format_ingest(report.ingest, report.ingest_errors),
        format_triage(report.triage),
        format_modqueue(report.modqueue),
    ])
