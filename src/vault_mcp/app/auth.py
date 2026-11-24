"""Who may call: GitHub OAuth (allow-listed logins) for interactive clients, named API keys
for headless automations. Also tracks per-tool metrics and turns domain errors into tool errors."""
from __future__ import annotations

import hmac
import re
import time

from fastmcp import Context
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware
from prometheus_client import Counter, Histogram

from .. import config

CALLS = Counter("vault_mcp_tool_calls_total", "Tool calls", ["tool", "client", "outcome"])
LATENCY = Histogram("vault_mcp_tool_seconds", "Tool latency", ["tool"])


class ApiKeys(TokenVerifier):
    """Static keys for headless clients; the key's name is the client's identity."""

    async def verify_token(self, token: str) -> AccessToken | None:
        for name, key in config.API_KEYS.items():
            if hmac.compare_digest(token.encode(), key.encode()):
                # The GitHub provider requires the "user" scope on every token MultiAuth accepts.
                return AccessToken(
                    token=token,
                    client_id=name,
                    scopes=["user"],
                    subject=name,
                    claims={"login": name, "api_key": True},
                )
        return None


def build_auth() -> AuthProvider | None:
    github = (
        GitHubProvider(
            client_id=config.GITHUB_CLIENT_ID,
            client_secret=config.GITHUB_CLIENT_SECRET,
            base_url=config.PUBLIC_URL,
            jwt_signing_key=config.JWT_SIGNING_KEY,
            require_authorization_consent=True,
        )
        if config.GITHUB_CLIENT_ID
        else None
    )
    keys = [ApiKeys()] if config.API_KEYS else []
    if github is None and not keys:
        return None
    return MultiAuth(server=github, verifiers=keys, base_url=config.PUBLIC_URL)


def client_name(ctx: Context | None) -> str:
    """API keys name their client; OAuth clients provide client_info or GitHub login."""
    token = get_access_token()
    if token is not None and token.claims.get("api_key"):
        return str(token.claims["login"])
    info = None
    if ctx is not None:
        try:
            info = ctx.session.client_params.client_info
        except (AttributeError, RuntimeError):
            info = None
    name = (info.name if info else None) or (token.claims.get("login") if token else None) or "local"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name)[:40]


class Gatekeeper(Middleware):
    """Allow-listed GitHub logins only, per-tool metrics, and domain errors as tool errors."""

    async def on_request(self, context, call_next):
        token = get_access_token()
        if (
            token is not None
            and not token.claims.get("api_key")
            and token.claims.get("login") not in config.ALLOWED_GITHUB_LOGINS
        ):
            raise ToolError("this GitHub account may not use the vault")
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        tool = getattr(context.message, "name", "?")
        client = client_name(context.fastmcp_context)
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except (ValueError, FileNotFoundError, OSError) as e:
            CALLS.labels(tool, client, "refused").inc()
            raise ToolError(str(e)) from None
        except Exception:
            CALLS.labels(tool, client, "error").inc()
            raise
        finally:
            LATENCY.labels(tool).observe(time.perf_counter() - start)
        CALLS.labels(tool, client, "ok").inc()
        return result
