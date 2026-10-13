"""Git access to the vault, serialised with flock(2) on .git/vault.lock.

Every writer (MCP server and worker) takes the same lock, so commits never race on
.git/index.lock and the worker never commits half of an agent's batch.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import subprocess
import threading
from pathlib import Path

from . import config

_thread_lock = threading.Lock()
IDEMPOTENCY_TRAILER = "Idempotency-Key"


class GitError(RuntimeError):
    pass


def git(*args: str, root: Path | None = None) -> str:
    root = root or config.VAULT_ROOT
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise GitError(f"git {args[0]}: {r.stderr.strip()[:400]}")
    return r.stdout


def rev_of(data: bytes | None) -> str | None:
    """A file's revision: its git blob hash, so it matches `git hash-object` and the history."""
    if data is None:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


@contextlib.contextmanager
def locked(root: Path | None = None):
    root = root or config.VAULT_ROOT
    with _thread_lock, open(root / ".git" / "vault.lock", "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def commit(paths: list[str], message: str, author: str, idempotency_key: str | None = None) -> str:
    """Stage paths (additions, edits and deletions) and commit them. Call inside locked()."""
    git("add", "-A", "--", *paths)
    if not git("status", "--porcelain", "--", *paths).strip():
        return "no changes"
    if idempotency_key:
        message += f"\n\n{IDEMPOTENCY_TRAILER}: {idempotency_key}"
    git("-c", f"user.name={author}", "-c", "user.email=vault-mcp@def4alt.com",
        "commit", "-q", "-m", message, "--", *paths)
    return git("rev-parse", "--short", "HEAD").strip()


def commit_for_key(key: str, depth: int = 500) -> tuple[str, list[str]] | None:
    """(short sha, paths) of a recent commit made with this idempotency key, if any."""
    out = git("log", f"-{depth}", "--fixed-strings", f"--grep={IDEMPOTENCY_TRAILER}: {key}",
              "--format=%h%x1f%B%x1e")
    for record in filter(str.strip, out.split("\x1e")):
        sha, body = record.strip().split("\x1f", 1)
        if f"{IDEMPOTENCY_TRAILER}: {key}" in body.splitlines():
            return sha, git("show", "--name-only", "--format=", sha).split()
    return None


def show(commit: str, path: str) -> str:
    """A file as it was at a commit."""
    return git("show", f"{commit}:{path}")
