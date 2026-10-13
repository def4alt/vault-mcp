"""Runtime settings, all from the environment so one image serves every role.

Values are read at call time (config.X), so tests can override them.
"""
from __future__ import annotations

import os
from pathlib import Path

env = os.environ.get

# Storage
VAULT_ROOT = Path(env("VAULT_ROOT", "/vault"))
DOCS_ROOT = Path(env("DOCS_ROOT", "/documents"))  # read everything; the worker writes the shared space
INBOX_ROOT = Path(env("INBOX_ROOT", "/inbox"))  # DOCS_ROOT/.inbox, mounted separately where needed
CACHE_ROOT = Path(env("CACHE_ROOT", "/cache"))  # derived text; safe to delete
SHARED_REL = env("SHARED_REL", "Agent/shared")

# File links (nginx verifies them) and limits
FILES_BASE_URL = env("FILES_BASE_URL", "http://localhost:8080").rstrip("/")
LINK_SECRET = env("LINK_SECRET", "")
LINK_TTL = int(env("LINK_TTL_SECONDS", "3600"))
QUOTA_BYTES = int(env("SHARED_QUOTA_BYTES", str(30 * 1024**3)))
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
