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


# Storage. The database is the vault. VAULT_ROOT is its Markdown export (a git repo, written after every
# change) plus blobs/, the stored files (content-addressed, not in git). Documents are read-only. The cache
# holds text extracts and the embedding model.
VAULT_ROOT = Path(env("VAULT_ROOT", "/vault"))
DOCS_ROOT = Path(env("DOCS_ROOT", "/documents"))
CACHE_ROOT = Path(env("CACHE_ROOT", "/cache"))  # text extracts + SQLite index
DROP_REL = env("DROP_REL", "Agent/inbox")  # drop folder inside the documents tree


def blobs_root() -> Path:
    return VAULT_ROOT / "blobs"


def incoming_root() -> Path:
    """Upload tickets and partial files, inside the vault mount so filing a blob is a rename."""
    return VAULT_ROOT / ".incoming"


DB_PATH = Path(env("DB_PATH", "/db/vault.db"))  # the vault itself: one SQLite file
# Embeddings: a Hugging Face repository with an ONNX export, run on the CPU. Empty model = hashed bag of words.
EMBED_MODEL = env("EMBED_MODEL", "onnx-community/harrier-oss-v1-0.6b-ONNX")  # Microsoft Harrier OSS v1 0.6B
EMBED_FILE = env("EMBED_FILE", "onnx/model.onnx")  # full precision: the fastest variant on a CPU without VNNI
EMBED_QUERY_PREFIX = env("EMBED_QUERY_PREFIX", "Instruct: Given a web search query, retrieve relevant passages "
                                               "that answer the query\nQuery: ")
EMBED_MAX_TOKENS = int(env("EMBED_MAX_TOKENS", "512"))
EMBED_THREADS = int(env("EMBED_THREADS", "4"))


def db_path() -> Path:
    return DB_PATH


# Public URL of this server: MCP at /mcp, OAuth, file links (/f, /d) and uploads (/u).
PUBLIC_URL = env("PUBLIC_URL", "http://localhost:8000").rstrip("/")
PAGES_URL = env("PAGES_URL", "").rstrip("/")  # where a person views HTML file notes (pages.py); empty = off
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
PUSH_INTERVAL = int(env("PUSH_INTERVAL_SECONDS", "900"))  # git push to the mirror; 0 = never
