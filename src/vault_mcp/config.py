"""Runtime configuration read via config.X so tests can override values easily."""
from __future__ import annotations

import os
from pathlib import Path

env = os.environ.get


def _pairs(text: str) -> dict[str, str]:
    """"opencode=abc,cron=def" -> {"opencode": "abc", "cron": "def"}."""
    return dict(item.split("=", 1) for item in filter(None, text.replace("\n", ",").split(",")) if "=" in item)


VAULT_ROOT = Path(env("VAULT_ROOT", "/vault"))
DOCS_ROOT = Path(env("DOCS_ROOT", "/documents"))  # Andrii's documents (Syncthing), read-only to the server
DOCS_SYNC_SECONDS = int(env("DOCS_SYNC_SECONDS", "300"))  # how often the documents index is refreshed; 0 = never
CACHE_ROOT = Path(env("CACHE_ROOT", "/cache"))  # embedding model and extracted document text
DB_PATH = Path(env("DB_PATH", "/db/vault.db"))
VEC_DB_PATH: Path | None = Path(v) if (v := env("VEC_DB_PATH")) else None
VIEWS_DIR = Path(env("VIEWS_DIR", str(VAULT_ROOT / "usr" / "views")))


def vec_db_path(db_path: Path | str | None = None) -> Path:
    """Resolve the rebuildable vectors database path beside `db_path` unless overridden globally."""
    if db_path is None and VEC_DB_PATH is not None:
        return VEC_DB_PATH
    base = Path(db_path) if db_path is not None else DB_PATH
    return base.with_name("vectors.db")


# Embeddings: Harrier OSS v1 0.6B ONNX CPU; empty string switches to hashed bag-of-words fallback.
EMBED_MODEL = env("EMBED_MODEL", "onnx-community/harrier-oss-v1-0.6b-ONNX")
EMBED_FILE = env("EMBED_FILE", "onnx/model.onnx")
EMBED_QUERY_PREFIX = env(
    "EMBED_QUERY_PREFIX",
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery: ",
)
EMBED_MAX_TOKENS = int(env("EMBED_MAX_TOKENS", "512"))
EMBED_THREADS = int(env("EMBED_THREADS", "4"))

# HTTP server & auth
PUBLIC_URL = env("PUBLIC_URL", "http://localhost:8000").rstrip("/")
PORT = int(env("PORT", "8000"))
METRICS_PORT = int(env("METRICS_PORT", "0"))
GITHUB_CLIENT_ID = env("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = env("GITHUB_CLIENT_SECRET", "")
JWT_SIGNING_KEY = env("JWT_SIGNING_KEY", "")
ALLOWED_GITHUB_LOGINS = set(filter(None, env("ALLOWED_GITHUB_LOGINS", "").split(",")))
API_KEYS = _pairs(env("API_KEYS", ""))
# Signed, expiring download links (/f/<blob>/<name>, /d/<path>), checked by /_verify for Caddy.
LINK_SECRET = env("LINK_SECRET", "")
LINK_TTL = int(env("LINK_TTL_SECONDS", str(7 * 86400)))
SQL_TIMEOUT_SECONDS = float(env("SQL_TIMEOUT_SECONDS", "5"))
