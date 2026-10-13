"""Who is calling: the MCP client's name (claude-ai, openai-mcp, hermes...) and the signed-in account."""
from __future__ import annotations

import datetime as dt
import re

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token


def client_name(ctx: Context | None) -> str:
    info = None
    if ctx is not None:
        try:
            info = ctx.session.client_params.client_info
        except (AttributeError, RuntimeError):
            info = None
    token = get_access_token()
    name = (info.name if info else None) or (token.claims.get("login") if token else None) or "local"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:40]


def author(ctx: Context | None) -> tuple[str, str]:
    """(git author name, short client name) for commits made on behalf of the caller."""
    name = client_name(ctx)
    return f"vault-mcp ({name})", name


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(timespec="seconds")
