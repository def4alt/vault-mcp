"""Reviewing facts with Andrii: a queue of unreviewed facts, shown as a widget where it is supported."""
from __future__ import annotations

import json

from fastmcp import FastMCP
from fastmcp.apps import AppConfig
from fastmcp.tools import ToolResult
from mcp import types as mt

from .. import index
from .knowledge import page_url
from .models import ReviewItem, ReviewQueue
from .widgets import REVIEW_URI

review = FastMCP("vault-review")

UNREVIEWED = {"kind": "fact", "status": "unreviewed"}


@review.tool(annotations=mt.ToolAnnotations(title="Review facts with Andrii", read_only_hint=True,
                                            destructive_hint=False, idempotent_hint=True, open_world_hint=False),
             app=AppConfig(resource_uri=REVIEW_URI), output_schema=ReviewQueue.model_json_schema())
def review_queue(limit: int = 10) -> ToolResult:
    """The oldest unreviewed facts. Hosts with MCP Apps show Confirm / Outdated buttons that Andrii clicks;
    otherwise go through them in conversation and record his answers with save(frontmatter=...)."""
    with index.connect() as conn:
        index.sync_vault(conn)
        rows, _ = index.search(conn, "", "notes", UNREVIEWED, order_by="as_of", descending=False,
                               limit=max(1, min(limit, 30)))
        total = conn.execute("SELECT count(*) FROM items WHERE kind = 'fact' AND status = 'unreviewed'").fetchone()[0]
    items = [ReviewItem(id=r["id"], title=r["title"], summary=_summary(r), as_of=r["as_of"], status=r["status"],
                        url=page_url(r["id"])) for r in rows]
    queue = ReviewQueue(items=items, remaining=max(0, total - len(items)))
    text = "\n".join([f"{len(items)} unreviewed facts (of {total}):"] +
                     [f"- {i.id}: {i.title}" + (f" (as of {i.as_of})" if i.as_of else "") for i in items])
    return ToolResult(content=[mt.TextContent(type="text", text=text)], structured_content=queue.model_dump())


def _summary(row) -> str | None:
    return json.loads(row["fm"] or "{}").get("summary")
