"""MCP server for the Markdown knowledge vault.

Claude.ai and ChatGPT sign in through GitHub (OAuth proxy with DCR/CIMD + PKCE);
Hermes uses a static bearer token that is only accepted on the tailnet hostname.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hmac
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.dependencies import get_access_token, get_http_request
from fastmcp.server.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from . import guard
from .lint import lint as run_lint
from .lint import render as render_lint
from .vault import Vault, split_frontmatter

ROOT = Path(os.environ.get("VAULT_ROOT", "/vault"))
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")
ALLOWED_LOGINS = set(filter(None, os.environ.get("ALLOWED_GITHUB_LOGINS", "").split(",")))
TAILNET_HOSTS = set(filter(None, os.environ.get("TAILNET_HOSTS", "").split(",")))
BEARER_SUBJECT = "hermes"

_write_lock = asyncio.Lock()


class BearerVerifier(TokenVerifier):
    """One static token for a headless agent; compared in constant time."""

    def __init__(self, token: str):
        super().__init__()
        self._token = token

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token.encode(), self._token.encode()):
            return None
        # Same scope the GitHub provider requires, otherwise MultiAuth answers 403.
        return AccessToken(token=token, client_id=BEARER_SUBJECT, scopes=["user"],
                           subject=BEARER_SUBJECT, claims={"login": BEARER_SUBJECT})


class OwnerOnly(Middleware):
    """Authentication proves who you are; this decides whether you may use the vault."""

    async def on_request(self, context, call_next):
        token = get_access_token()
        if token is None:  # stdio / local tests
            return await call_next(context)
        login = token.claims.get("login")
        if login == BEARER_SUBJECT:
            host = get_http_request().headers.get("host", "").split(":")[0]
            if host not in TAILNET_HOSTS:
                raise ToolError("bearer tokens are only accepted on the tailnet hostname")
        elif login not in ALLOWED_LOGINS:
            raise ToolError("this GitHub account may not use the vault")
        return await call_next(context)


def build_auth() -> AuthProvider | None:
    verifiers = [BearerVerifier(t)] if (t := os.environ.get("BEARER_TOKEN")) else []
    server = None
    if os.environ.get("GITHUB_CLIENT_ID"):
        server = GitHubProvider(
            client_id=os.environ["GITHUB_CLIENT_ID"],
            client_secret=os.environ["GITHUB_CLIENT_SECRET"],
            base_url=BASE_URL,
            jwt_signing_key=os.environ["JWT_SIGNING_KEY"],
            # Personal server: skip the extra consent screen, GitHub login is the gate.
            require_authorization_consent=False,
        )
    if server is None and not verifiers:
        return None
    return MultiAuth(server=server, verifiers=verifiers, base_url=BASE_URL)


mcp = FastMCP(
    "vault",
    instructions=(
        "Personal Markdown knowledge vault. Read AGENTS.md first (read('AGENTS')) and follow it: "
        "ingest raw material into sources/, distill one fact per note into notes/, link entities, "
        "append a line to log.md with append_log. Writes are validated and committed automatically."
    ),
    auth=build_auth(),
    middleware=[OwnerOnly()],
)


def vault() -> Vault:
    return Vault(ROOT)


def schema() -> dict:
    return yaml.safe_load((ROOT / ".schema/notes.yaml").read_text())


def git(*args: str) -> str:
    for attempt in range(5):
        r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout
        if "index.lock" not in r.stderr:
            raise ToolError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
        # The autocommit job holds the lock for a moment; wait it out.
        time.sleep(0.5 * (attempt + 1))
    raise ToolError("git is busy; try again")


def commit(paths: list[str], message: str) -> str:
    who = (get_access_token().claims.get("login") if get_access_token() else None) or "local"
    git("add", "--", *paths)
    if not git("status", "--porcelain", "--", *paths).strip():
        return "no changes"
    git("-c", f"user.name=vault-mcp ({who})", "-c", "user.email=vault-mcp@def4alt.com",
        "commit", "-q", "-m", f"{message} ({who})", "--", *paths)
    return git("rev-parse", "--short", "HEAD").strip()


def page_summary(p) -> dict[str, Any]:
    keep = ("kind", "status", "as_of", "reviewed", "categories", "summary")
    return {"path": p.path, "title": p.title, **{k: str(p.meta[k]) if k in ("as_of", "reviewed") else p.meta[k]
                                                  for k in keep if p.meta.get(k) not in (None, "", [])}}


@mcp.tool
def search(query: str, folder: str | None = None, limit: int = 20) -> list[dict]:
    """Full-text search over notes, sources and projects. Title and summary matches rank first.
    folder: optionally restrict to "notes", "sources" or "projects"."""
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    if not terms:
        raise ToolError("query needs at least one word")
    hits = []
    for p in vault().pages.values():
        if folder and not p.path.startswith(folder.rstrip("/") + "/"):
            continue
        head = f"{p.path} {p.title} {p.meta.get('summary', '')}".lower()
        body = p.body.lower()
        if not all(t in head or t in body for t in terms):
            continue
        score = sum(5 * head.count(t) + body.count(t) for t in terms)
        hits.append((score, p))
    hits.sort(key=lambda h: -h[0])
    return [page_summary(p) for _, p in hits[:limit]]


@mcp.tool
def read(path: str) -> str:
    """Read a page by vault path, e.g. "notes/dashpoint", "AGENTS", "index", "log", "views/stale"."""
    path = path.strip().removesuffix(".md").lstrip("/")
    f = (ROOT / f"{path}.md").resolve()
    if ROOT.resolve() not in f.parents or not f.is_file():
        raise ToolError(f"no page `{path}`")
    return f.read_text(encoding="utf-8")


@mcp.tool
def query(where: dict[str, Any] | None = None, folder: str = "notes", order_by: str = "as_of",
          descending: bool = False, limit: int = 50) -> list[dict]:
    """Filter pages by frontmatter. `where` maps field → value; list fields match if they contain
    the value; use "*" for "field is set" and null for "field is missing".
    Example: {"kind": "fact", "status": "unreviewed", "categories": "careers"}."""
    rows = []
    for p in vault().pages.values():
        if not p.path.startswith(folder.rstrip("/") + "/"):
            continue
        ok = True
        for key, want in (where or {}).items():
            have = p.meta.get(key)
            if want is None:
                ok = have in (None, "", [])
            elif want == "*":
                ok = have not in (None, "", [])
            elif isinstance(have, list):
                ok = want in have or f"[[{want}]]" in have
            else:
                ok = str(have) == str(want)
            if not ok:
                break
        if ok:
            rows.append(p)
    rows.sort(key=lambda p: str(p.meta.get(order_by) or ""), reverse=descending)
    return [page_summary(p) for p in rows[:limit]]


@mcp.tool
def backlinks(path: str) -> list[dict]:
    """Pages that link to `path` (in frontmatter or body). Use it to walk from an entity to its facts."""
    v = vault()
    path = path.strip().removesuffix(".md")
    return [page_summary(v.pages[b]) for b in v.backlinks(path)]


@mcp.tool
async def write_note(path: str, content: str, message: str) -> str:
    """Create or replace a page. `content` is the whole file: YAML frontmatter + Markdown body.
    Allowed: notes/<name>, projects/<name>, sources/<name> (sources: create only).
    The write is validated against AGENTS.md and .schema/notes.yaml, then committed with `message`."""
    path = guard.check_path(path)
    f = ROOT / f"{path}.md"
    async with _write_lock:
        old = f.read_text(encoding="utf-8") if f.exists() else None
        try:
            guard.check_write(path, content, old, schema())
        except guard.Refused as e:
            raise ToolError(str(e)) from None
        f.write_text(content.rstrip() + "\n", encoding="utf-8")
        sha = commit([f"{path}.md"], message)
    return f"{'updated' if old else 'created'} {path} ({sha})"


@mcp.tool
async def update_frontmatter(path: str, fields: dict[str, Any] | None = None,
                             unset: list[str] | None = None, message: str = "") -> str:
    """Change frontmatter fields of an existing note without rewriting its body.
    Example: fields={"status": "outdated"} on a note that a newer note supersedes."""
    path = guard.check_path(path)
    f = ROOT / f"{path}.md"
    if not f.exists():
        raise ToolError(f"no page `{path}`")
    async with _write_lock:
        old = f.read_text(encoding="utf-8")
        meta, body = split_frontmatter(old)
        for k, v in (fields or {}).items():
            meta[k] = dt.date.fromisoformat(v) if k in ("as_of", "reviewed") and isinstance(v, str) else v
        for k in unset or []:
            meta.pop(k, None)
        new = guard.render(meta, body)
        try:
            guard.check_write(path, new, old, schema())
        except guard.Refused as e:
            raise ToolError(str(e)) from None
        f.write_text(new, encoding="utf-8")
        sha = commit([f"{path}.md"], message or f"edit: frontmatter of {path}")
    return f"updated {path} ({sha})"


@mcp.tool
async def append_log(op: str, target: str, what: str, agent: str) -> str:
    """Append one line to log.md: `- <date> <op> <target>: <what> (<agent>)`.
    op: ingest | query | lint | edit | import. target: a wikilink like [[sources/x]]."""
    if op not in ("ingest", "query", "lint", "edit", "import"):
        raise ToolError("op must be ingest, query, lint, edit or import")
    line = f"- {dt.date.today()} {op} {target}: {what} ({agent})"
    if "\n" in line:
        raise ToolError("one line only")
    guard.check_secrets(line)
    async with _write_lock:
        with (ROOT / "log.md").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        sha = commit(["log.md"], f"log: {op} {target}")
    return f"logged ({sha})"


@mcp.tool
async def lint(write_report: bool = False) -> str:
    """Run the vault lint from AGENTS.md (conflicts, stale facts, orphans, undistilled sources,
    missing entities, schema violations, broken links). write_report=True saves notes/lint-<date>.md."""
    today = dt.date.today()
    text = render_lint(run_lint(vault(), today), today)
    if write_report:
        async with _write_lock:
            (ROOT / f"notes/lint-{today}.md").write_text(text, encoding="utf-8")
            commit([f"notes/lint-{today}.md"], f"lint: report {today}")
    return text


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok" if (ROOT / "AGENTS.md").exists() else "vault missing",
                             status_code=200 if (ROOT / "AGENTS.md").exists() else 503)


def main() -> None:
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), path="/mcp")


if __name__ == "__main__":
    main()
