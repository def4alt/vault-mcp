"""Git access to the vault, serialised with flock(2) on .git/vault.lock.

Every writer (MCP server, worker, autocommit and lint jobs) takes the same lock,
so commits never race on .git/index.lock.
"""
from __future__ import annotations

import contextlib
import fcntl
import subprocess
import threading
from pathlib import Path

from . import config

_thread_lock = threading.Lock()


class GitError(RuntimeError):
    pass


def git(*args: str, root: Path | None = None) -> str:
    root = root or config.VAULT_ROOT
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise GitError(f"git {args[0]}: {r.stderr.strip()[:400]}")
    return r.stdout


@contextlib.contextmanager
def locked(root: Path | None = None):
    root = root or config.VAULT_ROOT
    with _thread_lock, open(root / ".git" / "vault.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def commit(paths: list[str], message: str, author: str, root: Path | None = None) -> str:
    """Stage paths (additions, edits and deletions) and commit them. Call inside locked()."""
    git("add", "-A", "--", *paths, root=root)
    if not git("status", "--porcelain", "--", *paths, root=root).strip():
        return "no changes"
    git("-c", f"user.name={author}", "-c", "user.email=vault-mcp@def4alt.com",
        "commit", "-q", "-m", message, "--", *paths, root=root)
    return git("rev-parse", "--short", "HEAD", root=root).strip()
