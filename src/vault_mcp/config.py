"""Runtime settings, all from the environment so one image serves every role.

Values are read at call time (config.X), so tests can override them.
"""
from __future__ import annotations

import os
from pathlib import Path

env = os.environ.get

# Storage. The vault (git) holds notes and file notes; blobs/ holds the files themselves
# (content-addressed, not in git). Documents are read-only. The cache is derived and rebuildable.
VAULT_ROOT = Path(env("VAULT_ROOT", "/vault"))
DOCS_ROOT = Path(env("DOCS_ROOT", "/documents"))
INBOX_ROOT = Path(env("INBOX_ROOT", "/inbox"))  # HTTP uploads and fetches land here
CACHE_ROOT = Path(env("CACHE_ROOT", "/cache"))  # text extracts + SQLite index
DROP_REL = env("DROP_REL", "Agent/inbox")  # Syncthing drop folder inside the documents tree


def blobs_root() -> Path:
    return VAULT_ROOT / "blobs"


def index_path() -> Path:
    return CACHE_ROOT / "index.sqlite"


# Links (nginx verifies them) and limits
FILES_BASE_URL = env("FILES_BASE_URL", "http://localhost:8080").rstrip("/")
# The same nginx on the tailnet: no Cloudflare body limit, for agents with a shell on the tailnet (Hermes).
FILES_TAILNET_BASE_URL = env("FILES_TAILNET_BASE_URL", "").rstrip("/")
NOTES_BASE_URL = env("NOTES_BASE_URL", "http://localhost:3000").rstrip("/")  # SilverBullet, for citations
LINK_SECRET = env("LINK_SECRET", "")
LINK_TTL = int(env("LINK_TTL_SECONDS", "3600"))
CITATION_TTL = int(env("CITATION_TTL_SECONDS", str(7 * 86400)))  # document links handed out as citations
QUOTA_BYTES = int(env("FILES_QUOTA_BYTES", str(30 * 1024**3)))
FETCH_MAX_BYTES = int(env("FETCH_MAX_BYTES", str(1024**3)))

# MCP server and auth
PORT = int(env("PORT", "8000"))
METRICS_PORT = int(env("METRICS_PORT", "0"))  # 0 = off; never routed through the ingress
BASE_URL = env("BASE_URL", "http://localhost:8000")
GITHUB_CLIENT_ID = env("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = env("GITHUB_CLIENT_SECRET", "")
JWT_SIGNING_KEY = env("JWT_SIGNING_KEY", "")
ALLOWED_GITHUB_LOGINS = set(filter(None, env("ALLOWED_GITHUB_LOGINS", "").split(",")))
BEARER_TOKEN = env("BEARER_TOKEN", "")
TAILNET_HOSTS = set(filter(None, env("TAILNET_HOSTS", "").split(",")))

# Worker
WORKER_INTERVAL = int(env("WORKER_INTERVAL_SECONDS", "10"))
DROP_SETTLE_SECONDS = int(env("DROP_SETTLE_SECONDS", "30"))  # leave files Syncthing is still writing
