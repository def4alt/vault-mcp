"""Composition root: the MCP server (tools, auth, middleware) plus the plain HTTP routes for file bytes.

    /mcp              MCP (Streamable HTTP), OAuth routes next to it
    /u/<ticket>/<n>   PUT an upload (signed link); answers with the file note as JSON
    /_verify          Caddy's forward_auth check for /f and /d downloads, which Caddy serves from disk
    /healthz
"""
from __future__ import annotations

import urllib.parse
from functools import partial

import anyio
from fastmcp import FastMCP
from prometheus_client import start_http_server
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .. import config, documents, filing, incoming, signing
from .auth import Gatekeeper, build_auth
from .tools import vault

# Clients may only show the first ~500 characters: the essentials come first.
INSTRUCTIONS = (
    "Andrii's knowledge vault: Markdown pages in git (notes/, sources/, projects/, files/), linked with "
    "[[wikilinks]], plus his documents. Read with `search` then `fetch`; query anything structured (tags, links, "
    "history, problems, files) with `sql`. Change pages with `write` (one commit; pass base_rev) and `delete`. "
    "Add files with `upload`. Never put file bytes in tool calls: with a shell, use curl on the links.\n\n"
    "You are a trusted collaborator working alongside other agents (Claude, ChatGPT, Hermes) in the same vault. "
    "Capture what you learn, link it, describe and tag files you add. Conventions: fetch('AGENTS'). Hard limits: "
    "no secrets in the vault, sources stay as captured, frontmatter matches the schema (vault://schema)."
)
STATUS_TEXT = {403: "invalid link\n", 410: "link expired\n"}


async def verify(request: Request) -> Response:
    """Caddy asks before serving /f/... or /d/... from disk."""
    if request.headers.get("x-forwarded-method", "GET") not in ("GET", "HEAD"):
        return Response(status_code=405)
    uri = request.headers.get("x-forwarded-uri", "")
    if not uri.startswith(("/f/", "/d/")):
        return Response(status_code=403)
    status = signing.verify(uri)
    return Response(STATUS_TEXT.get(status), status_code=status)


async def put_file(request: Request) -> Response:
    """One file of an upload ticket, streamed to disk and filed before the response."""
    raw = request.scope["raw_path"].decode("latin-1") + "?" + request.scope["query_string"].decode("latin-1")
    status = signing.verify(raw)
    if status != 200:
        return PlainTextResponse(STATUS_TEXT[status], status_code=status)
    ticket = incoming.ticket(request.path_params["ticket"])
    if ticket is None:
        return PlainTextResponse("upload ticket expired\n", status_code=410)
    length = int(request.headers.get("content-length") or 0)
    if length > config.UPLOAD_MAX_BYTES:
        return PlainTextResponse(f"larger than {config.UPLOAD_MAX_BYTES // 1024**2} MB\n", status_code=413)
    try:
        await anyio.to_thread.run_sync(incoming.check_quota, length)
        part = await incoming.receive(request.stream(), config.UPLOAD_MAX_BYTES)
    except documents.DocumentError as e:
        return PlainTextResponse(f"{e}\n", status_code=413)
    name = documents.safe_name(urllib.parse.unquote(request.path_params["name"]))
    result = await anyio.to_thread.run_sync(partial(
        filing.file, part, original_name=name, origin=filing.origin_of(ticket["client"]), added_by=ticket["client"],
        purpose=ticket["purpose"], tags=ticket["tags"]))
    return JSONResponse({"note": result["note"], "duplicate": result["duplicate"]},
                        status_code=200 if result["duplicate"] else 201)


async def healthz(_: Request) -> PlainTextResponse:
    ok = (config.VAULT_ROOT / "AGENTS.md").exists()
    return PlainTextResponse("ok\n" if ok else "vault missing\n", status_code=200 if ok else 503)


def build() -> FastMCP:
    server = FastMCP("vault", instructions=INSTRUCTIONS, auth=build_auth(), middleware=[Gatekeeper()])
    server.mount(vault)
    server.custom_route("/_verify", methods=["GET", "HEAD"])(verify)
    server.custom_route("/u/{ticket}/{name}", methods=["PUT"])(put_file)
    server.custom_route("/healthz", methods=["GET"])(healthz)
    return server


def main() -> None:
    if config.METRICS_PORT:
        start_http_server(config.METRICS_PORT)  # its own port, never published
    build().run(transport="http", host="0.0.0.0", port=config.PORT, path="/mcp")
