"""Composition root: one FastMCP server with auth and middleware, the tool groups mounted on it."""
from __future__ import annotations

from fastmcp import FastMCP
from prometheus_client import start_http_server
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .. import config
from .auth import Gatekeeper, build_auth
from .files import shared_files
from .guidance import guidance
from .notes import notes

INSTRUCTIONS = (
    "Andrii's personal knowledge vault and documents. You are a trusted collaborator with full read access "
    "and broad write access: capture what you learn, keep the knowledge graph tidy, and share files with the "
    "other agents (Claude, ChatGPT, Hermes) through the shared space. Read AGENTS first (`read('AGENTS')`) "
    "for the conventions. Every change is a git commit: inspect with `history`, undo with `revert`, and "
    "record what you did with `append_log`."
)


def build() -> FastMCP:
    server = FastMCP("vault", instructions=INSTRUCTIONS, auth=build_auth(), middleware=[Gatekeeper()])
    for group in (notes, shared_files, guidance):
        server.mount(group)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> PlainTextResponse:
        ok = (config.VAULT_ROOT / "AGENTS.md").exists()
        return PlainTextResponse("ok" if ok else "vault missing", status_code=200 if ok else 503)

    return server


def main() -> None:
    if config.METRICS_PORT:
        start_http_server(config.METRICS_PORT)  # its own port, never routed through the ingress
    build().run(transport="http", host="0.0.0.0", port=config.PORT, path="/mcp")
