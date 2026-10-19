"""
Commit and push the knowledge data repo after a compile.

`daily/` and `knowledge/` may live in a separate private git repo that the
compiler reaches through symlinks (this installation: ../.claude-memory-data).
After each compile we commit whatever changed there and push, so the knowledge
base is backed up without a manual step.

Safety rules, in order:
1. Resolve symlinks and ask git for the repo toplevel of the data directory.
2. Never touch a repo whose toplevel is the compiler checkout itself
   (`forbidden_root`). For a user whose knowledge/ is a real directory inside
   the compiler, syncing would `git add -A` the code tree and push it to a
   public remote.
3. A plain directory, a missing `git`, or a push failure never fails the
   caller - the result is returned as a string for logging.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def find_data_repo(data_dir: Path, forbidden_root: Path) -> Path | None:
    """Return the git toplevel that owns `data_dir`, or None if there is none
    or if it is the compiler checkout."""
    real = Path(data_dir).resolve()
    if not real.exists():
        return None
    probe = _git(real, "rev-parse", "--show-toplevel")
    if probe.returncode != 0:
        return None
    top = Path(probe.stdout.strip()).resolve()
    if top == Path(forbidden_root).resolve():
        return None
    return top


def sync_data_repo(data_dir: Path, forbidden_root: Path, message: str | None = None) -> str:
    """Stage everything in the data repo, commit if anything changed, push.

    Returns a one-line status: "pushed <sha>", "nothing to commit",
    "committed <sha>, push failed: ...", or "skipped: <reason>".
    """
    try:
        repo = find_data_repo(data_dir, forbidden_root)
    except (OSError, subprocess.SubprocessError) as e:
        return f"skipped: {e}"
    if repo is None:
        return "skipped: no separate data repo (or it is the compiler checkout)"

    try:
        add = _git(repo, "add", "-A")
        if add.returncode != 0:
            return f"skipped: git add failed: {add.stderr.strip()}"

        status = _git(repo, "status", "--porcelain")
        if status.returncode != 0:
            return f"skipped: git status failed: {status.stderr.strip()}"
        if not status.stdout.strip():
            return "nothing to commit"

        if message is None:
            today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
            message = f"kb: compile {today}"
        commit = _git(repo, "commit", "-q", "-m", message)
        if commit.returncode != 0:
            return f"skipped: git commit failed: {commit.stderr.strip()}"
        sha = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()

        push = _git(repo, "push", "-q", timeout=300)
        if push.returncode != 0:
            return f"committed {sha}, push failed: {push.stderr.strip().splitlines()[-1] if push.stderr.strip() else 'unknown'}"
        return f"pushed {sha}"
    except (OSError, subprocess.SubprocessError) as e:
        return f"skipped: {e}"
