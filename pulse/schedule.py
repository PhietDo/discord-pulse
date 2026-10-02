"""launchd jobs on macOS: the pipeline every 30 minutes, a weekly digest, and optionally the bot.

API keys are never written into a plist. Each job loads an env file when it starts.
"""
from __future__ import annotations

import os
import plistlib
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

LABEL_PREFIX = "com.discordpulse"
DEFAULT_ENV_FILE = Path("~/.config/discord-pulse/env").expanduser()
AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


class ScheduleError(Exception):
    pass


@dataclass(frozen=True)
class Job:
    name: str
    command: str
    interval_seconds: int | None = None
    weekly: dict | None = None
    keep_alive: bool = False


JOBS = {
    "pipeline": Job("pipeline", "pipeline", interval_seconds=1800),
    "digest": Job("digest", "digest", weekly={"Weekday": 1, "Hour": 9, "Minute": 0}),
    "bot": Job("bot", "bot", keep_alive=True),
}


def label(name: str) -> str:
    return f"{LABEL_PREFIX}.{name}"


def build_plist(job: Job, *, python: Path, config: Path, env_file: Path, log_dir: Path) -> dict:
    env = shlex.quote(str(env_file))
    script = (
        f"set -a; [ -f {env} ] && . {env}; set +a; "
        f"exec {shlex.quote(str(python))} -m pulse.run --config {shlex.quote(str(config))} {job.command}"
    )
    log = str(log_dir / f"{job.name}.log")
    plist = {
        "Label": label(job.name),
        "ProgramArguments": ["/bin/zsh", "-c", script],
        "WorkingDirectory": str(config.parent),
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": {"PATH": _PATH, "PYTHONUNBUFFERED": "1"},
        "ProcessType": "Background",
    }
    if job.interval_seconds:
        plist["StartInterval"] = job.interval_seconds
        plist["RunAtLoad"] = True
    if job.weekly:
        plist["StartCalendarInterval"] = dict(job.weekly)
    if job.keep_alive:
        plist["KeepAlive"] = True
        plist["RunAtLoad"] = True
        plist["ThrottleInterval"] = 60
    return plist


def env_file_names(env_file: Path) -> dict[str, str]:
    """Names assigned in a shell env file (NAME=value or export NAME=value), read as plain lines."""
    names: dict[str, str] = {}
    if not env_file.exists():
        return names
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if sep and name.isidentifier() and value.strip("'\" "):
            names[name] = "set"
    return names


def run_launchctl(cmd: list[str]) -> int:
    return subprocess.run(cmd, capture_output=True).returncode


def _path(agents_dir: Path, name: str) -> Path:
    return agents_dir / f"{label(name)}.plist"


def install(
    names: list[str], *, python: Path, config: Path, env_file: Path, log_dir: Path,
    agents_dir: Path, runner: Callable[[list[str]], int], uid: int | None = None,
) -> list[Path]:
    uid = os.getuid() if uid is None else uid
    agents_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in names:
        path = _path(agents_dir, name)
        path.write_bytes(plistlib.dumps(
            build_plist(JOBS[name], python=python, config=config, env_file=env_file, log_dir=log_dir)
        ))
        runner(["launchctl", "bootout", f"gui/{uid}/{label(name)}"])  # replaces an older copy; harmless if none
        code = runner(["launchctl", "bootstrap", f"gui/{uid}", str(path)])
        if code != 0:
            raise ScheduleError(f"launchctl bootstrap failed for {path} (exit {code})")
        written.append(path)
    return written


def uninstall(
    names: list[str], *, agents_dir: Path, runner: Callable[[list[str]], int], uid: int | None = None
) -> list[Path]:
    uid = os.getuid() if uid is None else uid
    removed = []
    for name in names:
        runner(["launchctl", "bootout", f"gui/{uid}/{label(name)}"])
        path = _path(agents_dir, name)
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed
