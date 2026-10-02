"""CLI: python -m pulse.run [--config pulse.toml] <command>."""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import uvicorn

from pulse.agents.base import BudgetExceeded, LLMError
from pulse.agents.digest import run_digest
from pulse.agents.investigate import run_investigation
from pulse.agents.triage import run_triage
from pulse.agents.theme import run_themes
from pulse.config import ConfigError, load_config
from pulse.db import connect
from pulse.demo import SERVER_NAME, demo_config, seed_demo
from pulse.modqueue import list_open, refresh_mod_queue
from pulse.pipeline import (
    build_llm,
    format_digest,
    format_ingest,
    format_investigation,
    format_modqueue,
    format_queue,
    format_report,
    format_themes,
    format_triage,
    ingest,
    run_pipeline,
)
from pulse.store import sync_launches
from pulse.sources.file_source import FileSource
from pulse.web.app import create_app
from pulse.web.settings import WebSettings


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pulse")
    parser.add_argument("--config", default="pulse.toml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest", help="import export files from the imports folder")
    triage = sub.add_parser("triage", help="label untriaged messages")
    triage.add_argument("--since", type=date.fromisoformat, help="only messages on or after YYYY-MM-DD")
    triage.add_argument("--force", action="store_true", help="re-triage messages in range")
    sub.add_parser("modqueue", help="refresh the mod queue")
    queue = sub.add_parser("queue", help="list open mod queue items, highest priority first")
    queue.add_argument("--limit", type=int, default=20)
    sub.add_parser("pipeline", help="ingest, triage, refresh the mod queue, then update themes")
    sub.add_parser("themes", help="group labelled messages into recurring themes")
    digest = sub.add_parser("digest", help="write the weekly digest, or a launch digest with --launch")
    digest.add_argument("--launch", help="launch name from [[launches]] in pulse.toml")
    investigate = sub.add_parser("investigate", help="ask the Investigate agent a question")
    investigate.add_argument("question")
    investigate.add_argument("--theme", type=int, help="theme id to focus on")
    web = sub.add_parser("web", help="serve the dashboard on localhost")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8321)
    web.add_argument("--db", help="database file (default: pulse.toml's, or demo.db with --demo)")
    web.add_argument("--name", help="server name shown in the sidebar")
    web.add_argument("--demo", action="store_true", help="serve the demo database; no pulse.toml or API keys needed")
    seed = sub.add_parser("seed-demo", help="write a synthetic community to a demo database")
    seed.add_argument("--db", default="demo.db")
    return parser


def _serve(settings: WebSettings, host: str, port: int) -> int:
    print(f"Discord Pulse dashboard on http://{host}:{port}")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="info")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "triage" and args.force and not args.since:
        parser.error("triage --force requires --since")
    if args.command == "seed-demo":
        try:
            counts = seed_demo(args.db, datetime.now(timezone.utc))
        except FileExistsError as e:
            print(f"seed-demo: {e}", file=sys.stderr)
            return 2
        print(f"demo database written to {args.db}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        print(f"serve it with: python -m pulse.run web --demo --db {args.db}")
        return 0
    if args.command == "web" and args.demo:
        db = Path(args.db or "demo.db")
        if not db.exists():
            print(f"web: {db} not found; run `python -m pulse.run seed-demo` first", file=sys.stderr)
            return 2
        settings = WebSettings(
            db_path=db, config=demo_config(db, datetime.now(timezone.utc)),
            server_name=args.name or SERVER_NAME, demo=True,
        )
        return _serve(settings, args.host, args.port)
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(config.db_path)
    sync_launches(conn, config.launches)
    now = datetime.now(timezone.utc)

    if args.command == "ingest":
        stats, errors = ingest(conn, config, FileSource(config.imports_dir))
        print(format_ingest(stats, errors))
    elif args.command == "triage":
        since = datetime.combine(args.since, datetime.min.time(), timezone.utc) if args.since else None
        print(format_triage(run_triage(conn, build_llm(conn, config), since=since, force=args.force)))
    elif args.command == "modqueue":
        print(format_modqueue(refresh_mod_queue(conn, config, now)))
    elif args.command == "queue":
        print(format_queue(list_open(conn, args.limit), now))
    elif args.command == "pipeline":
        print(format_report(run_pipeline(conn, config, build_llm(conn, config), now=now)))
    elif args.command == "themes":
        print(format_themes(run_themes(conn, build_llm(conn, config), now)))
    elif args.command == "digest":
        try:
            result = run_digest(conn, build_llm(conn, config), now, launch=args.launch)
        except LookupError as e:
            print(f"digest: {e.args[0]}", file=sys.stderr)
            return 1
        except (BudgetExceeded, LLMError) as e:
            print(f"digest failed: {e}", file=sys.stderr)
            return 1
        print(format_digest(result, conn))
    elif args.command == "investigate":
        context = None
        if args.theme is not None:
            if conn.execute("SELECT 1 FROM themes WHERE id = ?", (args.theme,)).fetchone() is None:
                print(f"unknown theme {args.theme}", file=sys.stderr)
                return 1
            context = {"theme_id": args.theme}
        try:
            result = run_investigation(conn, build_llm(conn, config), args.question, now, context=context)
        except (BudgetExceeded, LLMError, ValueError) as e:
            print(f"investigation failed: {e}", file=sys.stderr)
            return 1
        print(format_investigation(result, conn))
    elif args.command == "web":
        conn.close()
        settings = WebSettings(
            db_path=Path(args.db) if args.db else config.db_path, config=config,
            server_name=args.name or "Discord server", llm_factory=build_llm,
        )
        return _serve(settings, args.host, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
