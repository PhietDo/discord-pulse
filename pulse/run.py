"""CLI: python -m pulse.run [--config pulse.toml] <command>."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone

from pulse.agents.triage import run_triage
from pulse.config import ConfigError, load_config
from pulse.db import connect
from pulse.modqueue import refresh_mod_queue
from pulse.pipeline import (
    build_llm, format_ingest, format_modqueue, format_report, format_triage, ingest, run_pipeline,
)
from pulse.sources.file_source import FileSource


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulse")
    parser.add_argument("--config", default="pulse.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="import export files from the imports folder")
    triage = sub.add_parser("triage", help="label untriaged messages")
    triage.add_argument("--since", type=date.fromisoformat, help="only messages on or after YYYY-MM-DD")
    triage.add_argument("--force", action="store_true", help="re-triage messages in range")
    sub.add_parser("modqueue", help="refresh the mod queue")
    sub.add_parser("pipeline", help="ingest, triage, then refresh the mod queue")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(config.db_path)
    now = datetime.now(timezone.utc)

    if args.command == "ingest":
        stats, errors = ingest(conn, config, FileSource(config.imports_dir))
        print(format_ingest(stats, errors))
    elif args.command == "triage":
        since = datetime.combine(args.since, datetime.min.time(), timezone.utc) if args.since else None
        print(format_triage(run_triage(conn, build_llm(conn, config), since=since, force=args.force)))
    elif args.command == "modqueue":
        print(format_modqueue(refresh_mod_queue(conn, config, now)))
    elif args.command == "pipeline":
        print(format_report(run_pipeline(conn, config, build_llm(conn, config), now=now)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
