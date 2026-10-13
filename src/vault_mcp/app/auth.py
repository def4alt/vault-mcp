"""Authentication (GitHub OAuth for cloud clients, a bearer token for Hermes),
access control, metrics and error translation."""
from __future__ import annotations

import hmac
import time

from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware
from prometheus_client import Counter, Histogram

from .. import config
from ..repo import GitError
from .identity import BEARER_SUBJECT, client_name

CALLS = Counter("vault_mcp_tool_calls_total", "Tool calls", ["tool", "client", "outcome"])
LATENCY = Histogram("vault_mcp_tool_seconds", "Tool latency", ["tool"])


class BearerVerifier(TokenVerifier):
    """One static token for a headless agent, compared in constant time."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode(), self._token.encode()):
            return None
        # The GitHub provider requires the "user" scope on every token MultiAuth accepts.
        return AccessToken(token=token, client_id=BEARER_SUBJECT, scopes=["user"],
                           subject=BEARER_SUBJECT, claims={"login": BEARER_SUBJECT})


def build_auth() -> AuthProvider | None:
    verifiers = [BearerVerifier(config.BEARER_TOKEN)] if config.BEARER_TOKEN else []
    server = GitHubProvider(
        client_id=config.GITHUB_CLIENT_ID,
        client_secret=config.GITHUB_CLIENT_SECRET,
        base_url=config.BASE_URL,
        jwt_signing_key=config.JWT_SIGNING_KEY,
        require_authorization_consent=False,  # personal server: the GitHub login is the gate
    ) if config.GITHUB_CLIENT_ID else None
    if server is None and not verifiers:
        return None
    return MultiAuth(server=server, verifiers=verifiers, base_url=config.BASE_URL)


class Gatekeeper(Middleware):
    """Who may use the vault, per-tool metrics, and domain errors as tool errors."""

    async def on_request(self, context, call_next):
        token = get_access_token()
        if token is not None:
            if token.claims.get("login") == BEARER_SUBJECT:
                host = get_http_request().headers.get("host", "").split(":")[0]
                if host not in config.TAILNET_HOSTS:
                    raise ToolError("bearer tokens are only accepted on the tailnet hostname")
            elif token.claims.get("login") not in config.ALLOWED_GITHUB_LOGINS:
                raise ToolError("this GitHub account may not use the vault")
        return await call_next(context)

    async def on_call_tool(self, context, call_next):
        tool = getattr(context.message, "name", "?")
        client = client_name(context.fastmcp_context)
        start = time.perf_counter()
        try:
            result = await call_next(context)
        except (ValueError, GitError, OSError) as e:  # rule violations, bad input, missing files
            CALLS.labels(tool, client, "refused").inc()
            raise ToolError(str(e)) from None
        except Exception:
            CALLS.labels(tool, client, "error").inc()
            raise
        finally:
            LATENCY.labels(tool).observe(time.perf_counter() - start)
        CALLS.labels(tool, client, "ok").inc()
        return result
