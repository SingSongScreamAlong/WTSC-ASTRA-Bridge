#!/usr/bin/env python3
"""WTSC ASTRA Bridge local Git synchronizer.

This process runs OUTSIDE Blender. It only synchronizes the exchange folders
between GitHub and the local clone. Blender itself never needs GitHub credentials.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_INTERVAL = 3.0


def run_git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed ({proc.returncode})\n"
            f"stdout: {proc.stdout.strip()}\n"
            f"stderr: {proc.stderr.strip()}"
        )
    return proc


def current_branch(repo: Path) -> str:
    return run_git(repo, "branch", "--show-current").stdout.strip() or "main"


def pull(repo: Path) -> None:
    branch = current_branch(repo)
    proc = run_git(
        repo,
        "pull",
        "--rebase",
        "--autostash",
        "origin",
        branch,
        check=False,
    )
    if proc.returncode != 0:
        # A transient conflict should not kill the daemon forever.
        print(f"[ASTRA] pull warning: {proc.stderr.strip() or proc.stdout.strip()}", file=sys.stderr)


def stage_exchange(repo: Path) -> bool:
    # Only stage the command/result transport surface. Never auto-commit source,
    # .blend files, config, or unrelated local edits.
    run_git(repo, "add", "-A", "--", "commands", "results", "previews", check=False)
    status = run_git(repo, "status", "--porcelain", "--", "commands", "results", "previews")
    return bool(status.stdout.strip())


def commit_and_push(repo: Path) -> None:
    if not stage_exchange(repo):
        return

    run_git(repo, "commit", "-m", "ASTRA: sync Blender results", check=False)
    branch = current_branch(repo)

    # Rebase once more in case ChatGPT posted a command during Blender execution.
    pull(repo)
    proc = run_git(repo, "push", "origin", branch, check=False)
    if proc.returncode != 0:
        print(f"[ASTRA] push warning: {proc.stderr.strip() or proc.stdout.strip()}", file=sys.stderr)


def validate_repo(repo: Path) -> None:
    if not (repo / ".git").exists():
        raise SystemExit(f"Not a git clone: {repo}")
    for relative in ("commands/inbox", "commands/processed", "results", "previews"):
        (repo / relative).mkdir(parents=True, exist_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="WTSC ASTRA Bridge sync daemon")
    parser.add_argument(
        "--repo",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="Path to the local WTSC-ASTRA-Bridge clone",
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    parser.add_argument("--once", action="store_true", help="Run one sync cycle and exit")
    args = parser.parse_args()

    repo = args.repo.expanduser().resolve()
    validate_repo(repo)

    print(f"[ASTRA] repo: {repo}")
    print(f"[ASTRA] interval: {args.interval:.1f}s")

    while True:
        try:
            pull(repo)
            commit_and_push(repo)
        except Exception as exc:
            print(f"[ASTRA] sync error: {exc}", file=sys.stderr)

        if args.once:
            break
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    main()
