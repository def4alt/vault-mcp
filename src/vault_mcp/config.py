"""Runtime settings, all from the environment so one image serves every role.

Values are read at call time (config.X), so tests can override them.
"""
from __future__ import annotations

import os
from pathlib import Path

env = os.environ.get


def _pairs(text: str) -> dict[str, str]:
    """"hermes=abc,ci=def" -> {"hermes": "abc", "ci": "def"}"""
    return dict(item.split("=", 1) for item in filter(None, text.replace("\n", ",").split(",")) if "=" in item)


# Storage. The vault (git) holds notes and file notes; blobs/ holds the files themselves
# (content-addressed, not in git). Documents are read-only. The cache is derived and rebuildable.
VAULT_ROOT = Path(env("VAULT_ROOT", "/vault"))
DOCS_ROOT = Path(env("DOCS_ROOT", "/documents"))
CACHE_ROOT = Path(env("CACHE_ROOT", "/cache"))  # text extracts + SQLite index
DROP_REL = env("DROP_REL", "Agent/inbox")  # drop folder inside the documents tree


def blobs_root() -> Path:
    return VAULT_ROOT / "blobs"


def incoming_root() -> Path:
    """Upload tickets and partial files, inside the vault mount so filing a blob is a rename."""
    return VAULT_ROOT / ".incoming"


def index_path() -> Path:
    return CACHE_ROOT / "index.sqlite"


# Public URL of this server: MCP at /mcp, OAuth, file links (/f, /d) and uploads (/u).
PUBLIC_URL = env("PUBLIC_URL", "http://localhost:8000").rstrip("/")
NOTES_BASE_URL = env("NOTES_BASE_URL", "http://localhost:3000").rstrip("/")  # SilverBullet: where a person reads a page
LINK_SECRET = env("LINK_SECRET", "")
LINK_TTL = int(env("LINK_TTL_SECONDS", "3600"))
CITATION_TTL = int(env("CITATION_TTL_SECONDS", str(7 * 86400)))  # document links handed out as citations
QUOTA_BYTES = int(env("FILES_QUOTA_BYTES", str(30 * 1024**3)))
UPLOAD_MAX_BYTES = int(env("UPLOAD_MAX_BYTES", str(8 * 1024**3)))
FETCH_MAX_BYTES = int(env("FETCH_MAX_BYTES", str(2 * 1024**3)))

# MCP server and auth
PORT = int(env("PORT", "8000"))
METRICS_PORT = int(env("METRICS_PORT", "0"))  # 0 = off; never published
GITHUB_CLIENT_ID = env("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = env("GITHUB_CLIENT_SECRET", "")
JWT_SIGNING_KEY = env("JWT_SIGNING_KEY", "")
ALLOWED_GITHUB_LOGINS = set(filter(None, env("ALLOWED_GITHUB_LOGINS", "").split(",")))
API_KEYS = _pairs(env("API_KEYS", ""))  # name=token for headless agents; the name is their identity
SQL_TIMEOUT_SECONDS = float(env("SQL_TIMEOUT_SECONDS", "5"))

# Worker
WORKER_INTERVAL = int(env("WORKER_INTERVAL_SECONDS", "10"))
DROP_SETTLE_SECONDS = int(env("DROP_SETTLE_SECONDS", "30"))  # leave files Syncthing is still writing
COMMIT_QUIET_SECONDS = int(env("COMMIT_QUIET_SECONDS", "60"))  # commit out-of-band edits once they settle
PUSH_INTERVAL = int(env("PUSH_INTERVAL_SECONDS", "900"))  # git push to the mirror; 0 = never
