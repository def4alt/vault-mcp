"""Who is calling, for commits, file notes and metrics."""
from __future__ import annotations

import datetime as dt
import re

from fastmcp import Context
from fastmcp.server.dependencies import get_access_token, get_http_request

from .. import config

BEARER_SUBJECT = "hermes"  # the claims login of the static bearer token


def client_name(ctx: Context | None) -> str:
    """The bearer token names Hermes; OAuth clients name themselves (claude-ai, openai-mcp, ...)."""
    token = get_access_token()
    if token is not None and token.claims.get("login") == BEARER_SUBJECT:
        return BEARER_SUBJECT
    info = None
    if ctx is not None:
        try:
            info = ctx.session.client_params.client_info
        except (AttributeError, RuntimeError):
            info = None
    name = (info.name if info else None) or (token.claims.get("login") if token else None) or "local"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:40]


def author(ctx: Context | None) -> tuple[str, str]:
    """(git author name, short client name) for commits made on behalf of the caller."""
    name = client_name(ctx)
    return f"vault-mcp ({name})", name


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(timespec="seconds")


def files_base() -> str:
    """Base URL for file links: the tailnet host for callers on the tailnet, else the public one."""
    try:
        host = get_http_request().headers.get("host", "").split(":")[0]
    except RuntimeError:  # no HTTP request (in-process clients)
        host = ""
    if config.FILES_TAILNET_BASE_URL and host in config.TAILNET_HOSTS:
        return config.FILES_TAILNET_BASE_URL
    return config.FILES_BASE_URL
