"""Composition root: one FastMCP server with auth and middleware, the tool groups mounted on it."""
from __future__ import annotations

from fastmcp import FastMCP
from prometheus_client import start_http_server
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .. import config
from .auth import Gatekeeper, build_auth
from .files import files
from .guidance import guidance
from .knowledge import knowledge
from .review import review
from .widgets import widgets

# Hosts may only read the first 512 characters: the essentials come first.
INSTRUCTIONS = (
    "Andrii's knowledge vault (Markdown + git) and his documents. Start with `search` (scope notes|sources|files|"
    "documents, filters like tags or links_to), then `fetch` an id. Change things with `save` (many pages, one commit),"
    " `move`, `delete`; every change is a commit you can `revert`. Files: `request_upload`, `save_from_url`, "
    "`share_file`, `get_link`. Conventions: fetch('AGENTS'). Record work with `log`.\n\n"
    "You are a trusted collaborator with broad write access, working alongside other agents (Claude, ChatGPT, "
    "Hermes) in the same vault. Capture what you learn, keep the graph tidy, describe and tag files you add, and "
    "check `history` before large changes. Hard limits: no secrets in the vault, sources stay as captured, "
    "frontmatter must match the schema (vault://schema)."
)


def build() -> FastMCP:
    server = FastMCP("vault", instructions=INSTRUCTIONS, auth=build_auth(), middleware=[Gatekeeper()])
    for group in (knowledge, files, review, widgets, guidance):
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
