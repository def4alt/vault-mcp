"""Document and sharing tools. Bytes never pass through the model: agents get text,
signed links, or ask the server to fetch; the worker files everything that arrives."""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp import types as mt

from .. import config, documents, sharing
from .identity import client_name, iso
from .models import FileEntry, FileHit, FileRef, FileText, Link, Upload, UploadStatus

READ = mt.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
RECEIVE = mt.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True)
URL_ELICITATION = mt.ClientCapabilities(elicitation=mt.ElicitationCapability(url=mt.UrlElicitationCapability()))

shared_files = FastMCP("vault-files")


def _entry(p: Path) -> FileEntry:
    rel = documents.relative(p)
    if p.is_dir():
        return FileEntry(path=rel, kind="dir")
    st = p.stat()
    return FileEntry(path=rel, kind=documents.kind_of(p), size=st.st_size, modified=iso(st.st_mtime),
                     has_text=bool(documents.cache_path(rel).exists() and documents.cache_path(rel).stat().st_size))


def _rg(*args: str) -> list[str]:
    out = subprocess.run(["rg", "--no-messages", *args], capture_output=True, text=True, timeout=30)
    return out.stdout.splitlines()


@shared_files.tool(annotations=READ)
def list_files(folder: str = "", recursive: bool = False, limit: int = 200) -> list[FileEntry]:
    """Browse the documents tree: Agent/, University/, Projects/, Personal/, Archive/.
    Agent/shared/ is where agents share files with each other."""
    root = documents.resolve(folder) if folder else config.DOCS_ROOT
    if not root.is_dir():
        raise ToolError(f"`{folder}` is not a folder")
    if recursive:
        paths = documents.walk(root)
    else:
        paths = (p for p in sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name)) if not p.name.startswith("."))
    entries = []
    for p in paths:
        entries.append(_entry(p))
        if len(entries) >= limit:
            break
    return entries


@shared_files.tool(annotations=READ)
def search_files(query: str, folder: str | None = None, limit: int = 20) -> list[FileHit]:
    """Search inside documents (PDF, Office, text) and in file names. Every word must match."""
    terms = query.split()
    if not terms:
        raise ToolError("query needs at least one word")
    text_root = config.CACHE_ROOT / "text"
    scope = text_root / folder.strip("/") if folder else text_root
    in_text: set[str] | None = None
    for t in terms:
        listed = _rg("-l", "-i", "-F", "-e", t, str(scope))
        hits = {line.removeprefix(f"{text_root}/").removesuffix(".txt") for line in listed}
        in_text = hits if in_text is None else in_text & hits
    base = documents.resolve(folder) if folder else config.DOCS_ROOT
    by_name = [documents.relative(p) for p in documents.walk(base) if all(t.lower() in p.name.lower() for t in terms)]
    results = []
    for rel in list(dict.fromkeys(by_name + sorted(in_text or [])))[:limit]:
        cache = documents.cache_path(rel)
        snippet = " ".join(_rg("-i", "-F", "-m", "1", "--no-filename", "-e", terms[0], str(cache))[:1])[:240] \
            if cache.exists() else ""
        results.append(FileHit(path=rel, kind=documents.kind_of(rel), snippet=" ".join(snippet.split())))
    return results


@shared_files.tool(annotations=READ)
def read_file(path: str, offset: int = 0, limit: int = 20000) -> FileText:
    """Text of a document (PDF, DOCX, PPTX, XLSX, OpenDocument, Markdown, code...), paged by characters.
    Media has no text; use `download_link` for it."""
    p = documents.resolve(path)
    if not p.is_file():
        raise ToolError(f"no file `{path}`")
    rel = documents.relative(p)
    text = documents.text_of(rel)
    if not text:
        raise ToolError(f"`{rel}` has no extractable text ({documents.kind_of(p)}); use download_link")
    end = offset + limit
    return FileText(path=rel, kind=documents.kind_of(p), size=p.stat().st_size, text=text[offset:end],
                    offset=offset, next_offset=end if end < len(text) else None, total_chars=len(text))


@shared_files.tool(annotations=READ)
def download_link(path: str, minutes: int = 60) -> Link:
    """A temporary HTTPS link to the original file, for you, another agent or Andrii."""
    p = documents.resolve(path)
    if not p.is_file():
        raise ToolError(f"no file `{path}`")
    url, expires = sharing.download_link(documents.relative(p), max(1, min(minutes, 1440)) * 60)
    return Link(url=url, expires_at=iso(expires))


@shared_files.tool(annotations=RECEIVE)
async def request_upload(purpose: str, ctx: Context) -> Upload:
    """Get files from Andrii into the shared space without passing them through the model.
    Clients that support it show him the upload page directly; otherwise give him the link.
    Afterwards check `upload_status` with the returned id."""
    sharing.check_quota()
    upload_id, _ = sharing.open_upload(client_name(ctx), purpose)
    url, expires = sharing.upload_link(upload_id)
    delivered = "link"
    if ctx.session.check_client_capability(URL_ELICITATION):
        result = await ctx.session.elicit_url(f"Upload files for: {purpose}", url, upload_id,
                                              related_request_id=ctx.request_id)
        delivered = f"elicitation:{result.action}"
    return Upload(id=upload_id, url=url, expires_at=iso(expires), delivered=delivered)


@shared_files.tool(annotations=RECEIVE)
def fetch_to_shared(url: str, purpose: str, ctx: Context, name: str | None = None) -> UploadStatus:
    """Download a public URL straight into the shared space. Waits briefly until the file is
    filed and returns its source note."""
    sharing.check_quota()
    upload_id, inbox = sharing.open_upload(client_name(ctx), purpose, url=url)
    sharing.fetch(url, inbox, name)
    return UploadStatus(**sharing.wait_until_ingested(upload_id))


@shared_files.tool(annotations=RECEIVE, meta={"openai/fileParams": ["file"]})
def share_file(file: FileRef, purpose: str, ctx: Context) -> UploadStatus:
    """Save a file attached in this chat to the shared space so every agent can use it."""
    sharing.check_quota()
    upload_id, inbox = sharing.open_upload(client_name(ctx), purpose)
    sharing.fetch(file.download_url, inbox, file.file_name)
    return UploadStatus(**sharing.wait_until_ingested(upload_id))


@shared_files.tool(annotations=READ)
def upload_status(id: str) -> UploadStatus:
    """What arrived for an upload or fetch id: filed files with their source notes, and files still waiting."""
    return UploadStatus(**sharing.upload_status(id))
