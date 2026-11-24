"""Vault MCP server: sql, recall, and file tools with MultiAuth + Gatekeeper middleware."""
from __future__ import annotations

import base64
import contextlib
import logging
import threading
from typing import Any

import anyio
from fastmcp import FastMCP
from mcp import types as mt
from prometheus_client import start_http_server
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .. import config, embed, files, store
from .auth import Gatekeeper, build_auth

log = logging.getLogger("vault-server")

INSTRUCTIONS = (
    "Andrii's personal knowledge vault on zorya. "
    "The database (`vault.db`) has 6 canonical tables: "
    "`items(id, kind, title, body, status, props, created_at, updated_at)`, "
    "`links(src, rel, dst, weight, status, note, updated_at)`, "
    "`cues(id, trigger_kind, item_id, on_item_id, due_at, action, status, created_at)`, "
    "`episodes(id, at, kind, source, context, detail, confidence, uri)`, "
    "`episode_items(episode_id, item_id, role)`, and "
    "`cursors(name, pos, updated_at)`. "
    "Built-in views: `v_active_items`, `v_due_cues`, `v_unconsolidated_episodes`, "
    "`v_cooccurrence`, `v_cooccurrence_window`, `v_concept_state`, `v_review_queue`, `v_study_next`. "
    "Vector tables in the rebuildable vector database are attached read-only as `vec` "
    "(`vec.items_vec`, `vec.episodes_vec`) with SQL functions `embed(text)` and `cosine(a, b)`.\n\n"
    "Tools:\n"
    "- `recall(query, budget=1800, limit=6)`: retrieve working memory (semantic vector hits, exact/opaque matches, "
    "1-hop attached neighbors, armed cues, and relevant episodes) within a token budget.\n"
    "- `sql(query, params?, limit=100, cursor?)`: run one parameterized SQL read or write statement; "
    "mutating statements delta-sync vector embeddings automatically.\n"
    "- `file(action, ...)`: manage files under `$VAULT_ROOT` (`read`, `write`, `put_blob`, `delete`).\n\n"
    "Resources: `vault://schema`, `vault://views`.\n"
    "Privacy: items with `json_extract(props, '$.private') = 1` are strictly private for helping Andrii directly; "
    "never quote or summarize them in CVs, emails, work docs, or external outputs."
)

vault = FastMCP("vault-tools")


def _annotations(title: str, *, read_only: bool, destructive: bool = False) -> mt.ToolAnnotations:
    return mt.ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=destructive)


@vault.tool(annotations=_annotations("Execute SQL query or statement", read_only=False, destructive=True))
async def sql(
    query: str,
    params: list[Any] | dict[str, Any] | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Run one SQL query or write statement against vault.db (5s timeout, 100-row cap, cursor pagination)."""
    return await anyio.to_thread.run_sync(
        lambda: store.run_sql(
            config.DB_PATH,
            query,
            params=params,
            limit=limit,
            cursor=cursor,
            vec_db_path=config.vec_db_path(),
        )
    )


@vault.tool(annotations=_annotations("Retrieve Working Memory Packet", read_only=True))
async def recall(query: str, budget: int = 1800, limit: int = 6) -> dict[str, Any]:
    """Retrieve relevant items, 1-hop context links, armed cues, and recent episodes within token budget."""
    return await anyio.to_thread.run_sync(
        lambda: store.recall(
            config.DB_PATH,
            query,
            budget=budget,
            k=limit,
            vec_db_path=config.vec_db_path(),
        )
    )


@vault.tool(annotations=_annotations("Read/write raw vault files", read_only=False, destructive=True))
async def file(
    action: str,
    path: str = "",
    content: str = "",
    content_base64: str = "",
    ext: str = "",
    offset: int = 0,
    limit: int = files.DEFAULT_READ_LIMIT,
) -> dict[str, Any]:
    """Operate strictly inside $VAULT_ROOT: action in read | write | put_blob | delete."""
    if action == "read":
        return await anyio.to_thread.run_sync(
            lambda: files.read_file(config.VAULT_ROOT, path, offset=offset, limit=limit)
        )
    if action == "write":
        res = await anyio.to_thread.run_sync(lambda: files.write_file(config.VAULT_ROOT, path, content))
        rel = str(res["path"])
        if rel.startswith("usr/views/") and rel.endswith(".sql"):
            await anyio.to_thread.run_sync(lambda: store.reload_views(config.DB_PATH, config.VIEWS_DIR))
        return res
    if action == "put_blob":
        data = base64.b64decode("".join(content_base64.split()), validate=True)
        return await anyio.to_thread.run_sync(lambda: files.put_blob(config.VAULT_ROOT, data, ext=ext))
    if action == "delete":
        res = await anyio.to_thread.run_sync(lambda: files.delete_file(config.VAULT_ROOT, path))
        rel = str(res["path"])
        if rel.startswith("usr/views/") and rel.endswith(".sql"):
            await anyio.to_thread.run_sync(lambda: store.reload_views(config.DB_PATH, config.VIEWS_DIR))
        return res
    raise ValueError(f"unsupported file action: {action}")


@vault.resource("vault://schema")
def resource_schema() -> str:
    """Canonical SQL schema and vector index DDL."""
    return store.schema_text()


@vault.resource("vault://views")
async def resource_views() -> str:
    """Currently active SQL view definitions."""
    return await anyio.to_thread.run_sync(lambda: store.views_text(config.DB_PATH))


async def healthz(_: Request) -> PlainTextResponse:
    def _check() -> bool:
        with contextlib.closing(store.connect(config.DB_PATH)) as conn:
            return (
                conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone()
                is not None
            )

    ok = await anyio.to_thread.run_sync(_check)
    return PlainTextResponse("ok\n" if ok else "vault not initialized\n", status_code=200 if ok else 503)


def build() -> FastMCP:
    """Construct the configured FastMCP server."""
    server = FastMCP("vault", instructions=INSTRUCTIONS, auth=build_auth(), middleware=[Gatekeeper()])
    server.mount(vault)
    server.custom_route("/healthz", methods=["GET"])(healthz)
    return server


def _startup_sync() -> None:
    try:
        embed.query("warm up")
        synced = store.sync_vectors(config.DB_PATH, vec_db_path=config.vec_db_path())
        log.info("background vector warmup complete (%d rows synced)", synced)
    except Exception:
        log.exception("background vector warmup failed")


def main() -> None:
    store.init_db(config.DB_PATH, views_dir=config.VIEWS_DIR, vec_db_path=config.vec_db_path())
    threading.Thread(target=_startup_sync, daemon=True).start()
    if config.METRICS_PORT:
        start_http_server(config.METRICS_PORT)
    build().run(transport="http", host="0.0.0.0", port=config.PORT, path="/mcp")
