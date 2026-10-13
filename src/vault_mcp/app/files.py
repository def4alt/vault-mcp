"""File tools. Bytes never pass through the model: agents with a shell move them with curl over signed
links, others ask the server to fetch, and people get an upload widget or page. The worker files
whatever arrives as a blob plus a file note."""
from __future__ import annotations

import shlex
import urllib.parse

from fastmcp import Context, FastMCP
from fastmcp.apps import AppConfig
from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp import types as mt

from .. import config, documents, sharing
from ..vault import read_page
from .identity import client_name, files_base, iso
from .models import FileRef, Link, Upload, UploadStatus
from .widgets import UPLOAD_URI

files = FastMCP("vault-files")


def _tool(title: str, *, read_only: bool, open_world: bool = False) -> mt.ToolAnnotations:
    return mt.ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=False,
                              idempotent_hint=read_only, open_world_hint=open_world)


def _new_upload(ctx: Context, purpose: str, tags: list[str] | None, example: str) -> Upload:
    sharing.check_quota()
    upload_id, _ = sharing.open_upload(client_name(ctx), purpose, tags=tags)
    page, template, expires = sharing.upload_links(upload_id, files_base())
    put = template.replace("{filename}", urllib.parse.quote(sharing.safe_name(example), safe=""))
    return Upload(id=upload_id, page_url=page, put_url_template=template, expires_at=iso(expires),
                  curl=f"curl -fsS -T {shlex.quote(example)} {shlex.quote(put)}")


@files.tool(annotations=_tool("Get a download link", read_only=True))
def get_link(id: str, minutes: int = 60) -> Link:
    """A temporary HTTPS link to the original of a file note ("files/x") or a document ("doc:University/x.pdf").
    With a shell, download it (see `curl`) and work on the file locally instead of paging text through fetch."""
    ttl = max(1, min(minutes, 7 * 1440)) * 60
    if id.startswith("files/"):
        if not (config.VAULT_ROOT / f"{id}.md").exists():
            raise ToolError(f"no file note `{id}`")
        meta = read_page(config.VAULT_ROOT, id).meta
        name = str(meta.get("original_name") or meta["blob"])
        url, expires = sharing.blob_link(str(meta["blob"]), name, files_base(), ttl)
    else:
        p = documents.resolve(id.removeprefix("doc:"))
        if not p.is_file():
            raise ToolError(f"no document `{id}`")
        name = p.name
        url, expires = sharing.document_link(documents.relative(p), files_base(), ttl)
    return Link(url=url, expires_at=iso(expires), curl=f"curl -fsSL -o {shlex.quote(name)} {shlex.quote(url)}")


@files.tool(annotations=_tool("Get an upload link", read_only=False, open_world=True))
def upload_link(purpose: str, ctx: Context, tags: list[str] | None = None,
                filename: str = "file.pdf") -> Upload:
    """For agents with a shell (a sandbox, a terminal): put files you have or made into the vault yourself.
    PUT each file to put_url_template with {filename} replaced, e.g. the returned `curl` command; then call
    upload_status(id). Never paste file contents into tool calls. Every file becomes files/<name>."""
    return _new_upload(ctx, purpose, tags, filename)


@files.tool(annotations=_tool("Ask Andrii for files", read_only=False, open_world=True),
            app=AppConfig(resource_uri=UPLOAD_URI), output_schema=Upload.model_json_schema())
def request_upload(purpose: str, ctx: Context, tags: list[str] | None = None) -> ToolResult:
    """For files only Andrii has. Hosts with MCP Apps show an upload widget; otherwise give him page_url.
    Each file becomes files/<name> (tagged with `tags`); check progress with upload_status(id)."""
    upload = _new_upload(ctx, purpose, tags, "file")
    text = (f"Upload page for Andrii (valid until {upload.expires_at}): {upload.page_url}\n"
            f"Files dropped there become file notes; then call upload_status(id='{upload.id}').")
    return ToolResult(content=[mt.TextContent(type="text", text=text)], structured_content=upload.model_dump())


@files.tool(annotations=_tool("Save a file from a URL", read_only=False, open_world=True))
def save_from_url(url: str, purpose: str, ctx: Context, tags: list[str] | None = None,
                  name: str | None = None) -> UploadStatus:
    """Download a public URL straight into the vault as a file note (the server fetches it).
    Waits until it is filed and returns the note."""
    sharing.check_quota()
    upload_id, inbox = sharing.open_upload(client_name(ctx), purpose, url=url, tags=tags)
    sharing.fetch(url, inbox, name)
    return UploadStatus(**sharing.wait_until_ingested(upload_id))


@files.tool(annotations=_tool("Save an attached file", read_only=False, open_world=True),
            meta={"openai/fileParams": ["file"]})
def share_file(file: FileRef, purpose: str, ctx: Context, tags: list[str] | None = None) -> UploadStatus:
    """Save a file attached in this chat into the vault (ChatGPT passes attachments by reference)."""
    sharing.check_quota()
    upload_id, inbox = sharing.open_upload(client_name(ctx), purpose, tags=tags)
    sharing.fetch(file.download_url, inbox, file.file_name)
    return UploadStatus(**sharing.wait_until_ingested(upload_id))


@files.tool(annotations=_tool("Check an upload", read_only=True), app=AppConfig(visibility=["model", "app"]))
def upload_status(id: str) -> UploadStatus:
    """Files that arrived for an upload or fetch id, with their file notes, and files still waiting."""
    return UploadStatus(**sharing.upload_status(id))
