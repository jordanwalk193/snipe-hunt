"""Optional: keep the data folder in step with a git remote.

If data/ is a git repository, the bot pulls before it edits the inventory and
commits (and pushes, when there is a remote) after. If it is not, every
function here quietly does nothing.
"""
from __future__ import annotations

import fcntl
import subprocess
from contextlib import contextmanager

from . import config

LOCK_FILE = "git.lock"


@contextmanager
def locked():
    """One git command sequence at a time: jobs and buttons share the working copy."""
    with open(config.ensure_state_dir() / LOCK_FILE, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def _git(*args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", "-C", str(config.DATA_DIR), *args],
                          capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def enabled() -> bool:
    return (config.DATA_DIR / ".git").exists()


def _has_remote() -> bool:
    return bool(_git("remote", check=False))


def pull() -> None:
    if not enabled():
        return
    with locked():
        if _has_remote():
            _git("pull", "--rebase", "--autostash", "-q")


def commit_and_push(paths: list[str], message: str) -> bool:
    """Commit the given paths if they changed. Never force-pushes."""
    if not enabled():
        return False
    with locked():
        _git("add", "--", *paths)
        if not _git("diff", "--cached", "--name-only"):
            return False
        _git("commit", "-q", "-m", message)
        if _has_remote():
            try:
                _git("push", "-q")
            except RuntimeError:
                _git("pull", "--rebase", "--autostash", "-q")   # someone else pushed first
                _git("push", "-q")
        return True
