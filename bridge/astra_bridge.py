#!/usr/bin/env python3
"""Synchronize the ASTRA command bus without giving Blender Git credentials."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_INTERVAL = 3.0
EXCHANGE_PATHS = ("commands", "results", "previews")
STATUS_FILE = Path("runtime/bridge_status.json")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    branch = run_git(repo, "branch", "--show-current").stdout.strip()
    if not branch:
        raise RuntimeError("Bridge repo has a detached HEAD; check out its normal branch")
    return branch


def git_path(repo: Path, name: str) -> Path:
    path = Path(run_git(repo, "rev-parse", "--git-path", name).stdout.strip())
    return path if path.is_absolute() else repo / path


def ensure_no_git_operation(repo: Path) -> None:
    for name in ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD"):
        if git_path(repo, name).exists():
            raise RuntimeError(f"Git {name} is in progress; resolve or abort it before syncing")


def pull(repo: Path, branch: str) -> bool:
    """Fetch commands, fast-forward when possible, rebase only if divergent."""
    ensure_no_git_operation(repo)
    before = run_git(repo, "rev-parse", "HEAD").stdout.strip()
    run_git(repo, "fetch", "origin", branch)
    remote_ref = f"origin/{branch}"
    remote_head = run_git(repo, "rev-parse", remote_ref).stdout.strip()
    if remote_head == before:
        return False

    def is_ancestor(old: str, new: str) -> bool:
        check = run_git(repo, "merge-base", "--is-ancestor", old, new, check=False)
        if check.returncode not in (0, 1):
            raise RuntimeError(f"git merge-base failed: {check.stderr.strip()}")
        return check.returncode == 0

    if is_ancestor(before, remote_head):
        # A fast-forward keeps unrelated staged edits staged. Rebase/autostash
        # would unnecessarily flatten the user's index on every remote command.
        run_git(repo, "merge", "--ff-only", remote_ref)
        return True
    if is_ancestor(remote_head, before):
        return False

    # True divergence can happen when a command arrives just as Blender
    # publishes results. Rebase only the local result commits.
    proc = run_git(repo, "rebase", "--autostash", remote_ref, check=False)
    if proc.returncode != 0:
        if git_path(repo, "rebase-merge").exists() or git_path(repo, "rebase-apply").exists():
            abort = run_git(repo, "rebase", "--abort", check=False)
            if abort.returncode != 0:
                raise RuntimeError(
                    f"Git rebase failed and could not be aborted: {proc.stderr.strip()}; "
                    f"abort error: {abort.stderr.strip()}"
                )
        raise RuntimeError(f"Git rebase failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return run_git(repo, "rev-parse", "HEAD").stdout.strip() != before


def stage_exchange(repo: Path) -> bool:
    run_git(repo, "add", "-A", "--", *EXCHANGE_PATHS)
    staged = run_git(repo, "diff", "--cached", "--quiet", "--", *EXCHANGE_PATHS, check=False)
    if staged.returncode not in (0, 1):
        raise RuntimeError(f"git diff --cached failed: {staged.stderr.strip()}")
    return staged.returncode == 1


def commit_exchange(repo: Path) -> bool:
    if not stage_exchange(repo):
        return False
    # --only prevents unrelated pre-staged source files from entering the
    # transport commit. A plain git commit would publish them.
    run_git(repo, "commit", "--only", "-m", "ASTRA: sync Blender results", "--", *EXCHANGE_PATHS)
    return True


def push(repo: Path, branch: str) -> bool:
    ahead = int(run_git(repo, "rev-list", "--count", f"origin/{branch}..HEAD").stdout.strip())
    if ahead == 0:
        return False
    run_git(repo, "push", "origin", branch)
    return True


def sync_once(repo: Path) -> dict:
    """One transport cycle. Local results persist before network work."""
    ensure_no_git_operation(repo)
    branch = current_branch(repo)
    committed = commit_exchange(repo)
    pulled = pull(repo, branch)
    pushed = push(repo, branch)
    return {
        "branch": branch,
        "committed": committed,
        "pulled": pulled,
        "pushed": pushed,
        "head": run_git(repo, "rev-parse", "HEAD").stdout.strip(),
        "pending_commands": len(list((repo / "commands" / "inbox").glob("*.json"))),
    }


def write_status(repo: Path, status: dict) -> None:
    """Publish a local heartbeat atomically; runtime/ is Git-ignored."""
    destination = repo / STATUS_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, destination)


def validate_repo(repo: Path) -> None:
    if not repo.is_dir() or run_git(repo, "rev-parse", "--show-toplevel", check=False).returncode != 0:
        raise SystemExit(f"Not a git clone: {repo}")
    toplevel = Path(run_git(repo, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if toplevel != repo:
        raise SystemExit(f"Bridge repo must be its Git root: {toplevel}")
    if not run_git(repo, "remote", "get-url", "origin", check=False).stdout.strip():
        raise SystemExit("Bridge repo has no origin remote")
    for relative in ("commands/inbox", "commands/processed", "results", "previews", "runtime"):
        (repo / relative).mkdir(parents=True, exist_ok=True)


def main() -> int:
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

    print(f"[ASTRA] repo: {repo}", flush=True)
    print(f"[ASTRA] interval: {args.interval:.1f}s", flush=True)
    status = {
        "bridge_version": "0.2.0",
        "service": "git_sync",
        "started_at": utc_now(),
        "consecutive_failures": 0,
        "last_success_at": None,
    }
    previous_report = None

    while True:
        status["heartbeat_at"] = utc_now()
        status["state"] = "syncing"
        write_status(repo, status)
        try:
            details = sync_once(repo)
            status.update(details)
            status["state"] = "ready"
            status["last_success_at"] = utc_now()
            status["last_error"] = None
            status["consecutive_failures"] = 0
            report = (details["committed"], details["pulled"], details["pushed"])
            if any(report) or previous_report != report:
                print(
                    f"[ASTRA] ready: branch={details['branch']} "
                    f"committed={details['committed']} pulled={details['pulled']} "
                    f"pushed={details['pushed']} pending={details['pending_commands']}",
                    flush=True,
                )
            previous_report = report
        except Exception as exc:
            status["state"] = "error"
            status["last_error"] = str(exc)
            status["consecutive_failures"] += 1
            print(f"[ASTRA] sync error: {exc}", file=sys.stderr, flush=True)
            previous_report = None
        finally:
            status["heartbeat_at"] = utc_now()
            write_status(repo, status)

        if args.once:
            return 0 if status["state"] == "ready" else 1
        time.sleep(max(1.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
