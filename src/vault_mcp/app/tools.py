"""The six tools: search, fetch and sql to read; write and delete to change; upload to bring files in.

Pages are Markdown files in git; files are content-addressed blobs described by file notes;
everything is linked with wikilinks and mirrored into a read-only SQLite index.
"""
from __future__ import annotations

import datetime as dt
import re
import shlex
import urllib.parse
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import yaml
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp import types as mt

from .. import config, documents, filing, incoming, index, repo, rules, signing
from ..vault import DATE_KEYS, PAGE_DIRS, render, split_frontmatter
from .auth import author, client_name
from .models import (
    Change,
    Document,
    FileRef,
    FrontmatterPatch,
    SearchResult,
    SearchResults,
    SqlResult,
    UploadResult,
    WriteResult,
)

vault = FastMCP("vault-tools")
TEXT_PAGE = 20_000  # characters per fetch


def _annotations(title: str, *, read_only: bool, destructive: bool = False, idempotent: bool = True,
                 open_world: bool = False) -> mt.ToolAnnotations:
    return mt.ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=destructive,
                              idempotent_hint=idempotent, open_world_hint=open_world)


def _url(item_id: str) -> str:
    """Where a person opens an item: SilverBullet for pages, a long-lived signed link for documents."""
    if item_id.startswith("doc:"):
        try:
            return signing.document_link(item_id.removeprefix("doc:"), config.CITATION_TTL)
        except documents.DocumentError:
            pass
    return f"{config.NOTES_BASE_URL}/{urllib.parse.quote(item_id.removeprefix('doc:'))}"


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(timespec="seconds")


# --- reading ---------------------------------------------------------------------------

@vault.tool(annotations=_annotations("Search the vault", read_only=True))
def search(query: str, limit: int = 10, cursor: str | None = None) -> SearchResults:
    """Ranked full-text search over notes, sources, projects, file notes (including the text inside the
    files) and documents. Words match as prefixes, "quoted phrases" exactly. Pages with all the words
    come first; if none has all of them, pages with any are ranked. Then fetch(id). For filters, tags,
    links, dates or counts use `sql`."""
    offset = index.decode_cursor(cursor)
    with index.connect() as conn:
        index.sync_vault(conn)  # a stat scan: picks up edits made in Obsidian or SilverBullet
        rows, more = index.search(conn, query, max(1, min(limit, 50)), offset)
    return SearchResults(
        results=[SearchResult(id=r["id"], title=r["title"] or r["id"], text=" ".join((r["snippet"] or "").split()),
                              url=_url(r["id"])) for r in rows],
        next_cursor=index.encode_cursor(offset + len(rows)) if more else None)


def _vault_file(item_id: str) -> Path | None:
    root = config.VAULT_ROOT.resolve()
    for candidate in (item_id, f"{item_id}.md"):
        f = (root / candidate).resolve()
        if root in f.parents and f.is_file() and not any(p.startswith(".") for p in Path(candidate).parts):
            return f
    return None


def _closest(wanted: str) -> str:
    with index.connect() as conn:
        index.sync_vault(conn)
        return ", ".join(index.suggest(conn, wanted)) or "none"


def _page(text: str, offset: int) -> tuple[str, dict]:
    end = offset + TEXT_PAGE
    return text[offset:end], ({"next_offset": end} if end < len(text) else {})


@vault.tool(annotations=_annotations("Read a page, file or document", read_only=True))
def fetch(id: str, offset: int = 0) -> Document:
    """Full text of anything search or sql returned: a page (notes/x, files/x, AGENTS, index, views/x),
    a page's stable uid, a document (doc:University/x.pdf), or an old version (notes/x@<commit>).
    File notes include the extracted text of their file and a download_url. metadata.rev is what
    you pass as base_rev to write. Long texts continue at metadata.next_offset."""
    item_id = id.strip().lstrip("/")
    if item_id.startswith("doc:"):
        p = documents.resolve(item_id.removeprefix("doc:"))
        if not p.is_file():
            raise ToolError(f"no document `{item_id}`; closest: {_closest(item_id)}")
        text = documents.cached_text(documents.sha256_of(p)) or documents.extract_text(p) or \
            f"({documents.media_of(p)} without extractable text; open it with the url)"
        body, more = _page(text, offset)
        return Document(id=item_id, title=p.name, text=body, url=_url(item_id),
                        metadata={"media": documents.media_of(p), "mime": documents.mime_of(p),
                                  "size": p.stat().st_size, **more})
    if "@" in item_id:
        path, commit = item_id.rsplit("@", 1)
        text = repo.show(commit, rules.normalise_path(path))
        body, more = _page(text, offset)
        return Document(id=item_id, title=path, text=body, url=_url(path), metadata={"commit": commit, **more})
    f = _vault_file(item_id)
    if f is None:
        with index.connect() as conn:
            index.sync_vault(conn)
            uid_path = index.by_uid(conn, item_id)
        f = _vault_file(uid_path) if uid_path else None
    if f is None:
        raise ToolError(f"no page `{item_id}`; closest: {_closest(item_id)}")
    data = f.read_bytes()
    item_id = str(f.relative_to(config.VAULT_ROOT.resolve())).removesuffix(".md")
    text = data.decode("utf-8", "replace")
    fm, body = split_frontmatter(text)
    meta: dict[str, Any] = {k: str(v) if isinstance(v, dt.date) else v for k, v in fm.items()}
    meta["rev"] = repo.rev_of(data)
    with index.connect() as conn:
        meta["backlinks"] = index.backlinks(conn, item_id)[:50]
    if item_id.startswith("files/") and fm.get("blob"):
        blob = config.VAULT_ROOT / str(fm["blob"])
        if blob.exists():
            meta["download_url"] = signing.blob_link(str(fm["blob"]), str(fm.get("original_name") or blob.name))
            extracted = documents.text_for(blob, str(fm.get("sha256") or documents.sha256_of(blob)))
            if extracted:
                text += "\n\n## Extracted text\n\n" + extracted
    title = (re.search(r"^# (.+)$", body, re.M) or [None, item_id])[1]
    page, more = _page(text, offset)
    return Document(id=item_id, title=title, text=page, url=_url(item_id), metadata={**meta, **more})


@vault.tool(annotations=_annotations("Query the index with SQL", read_only=True))
def sql(sql: str, params: list[Any] | None = None, limit: int = 100, cursor: str | None = None) -> SqlResult:
    """Read-only SQLite over the vault index: one SELECT/WITH, 5 s, rows as objects, ? params.
    notes(id=path, uid, folder, kind, status, title, summary, tags JSON, as_of, reviewed, blob, sha256, rev,
    mtime, size, fm=frontmatter JSON); documents(id 'doc:<path>', path, folder, name, media, mime, size);
    links(src, dst, field: body|embed|source|about|related|supersedes|projects); tags(id, tag);
    facets(id, field, value); files(id, name, mime, size, tags, summary, blob, url=download link);
    fts(id, title, summary, body): FTS5 incl. text inside files; commits(sha, date, author, message);
    commit_files(sha, id); problems(kind, id, detail). link(blob_or_doc_id) signs a download URL.
    E.g. SELECT src FROM links WHERE dst = 'notes/acme' | SELECT id FROM tags WHERE tag = 'slides' |
    SELECT id FROM notes WHERE kind = 'fact' AND status = 'unreviewed' | SELECT id, snippet(fts, 3, '[',
    ']', '…', 12) FROM fts WHERE fts MATCH 'transformer' ORDER BY bm25(fts). Long cells are clipped."""
    with index.connect() as conn:
        index.sync_vault(conn)
    offset = index.decode_cursor(cursor)
    columns, rows, more = index.run_sql(sql, params, max(1, min(limit, 500)), offset)
    return SqlResult(columns=columns, rows=rows, truncated=more,
                     next_cursor=index.encode_cursor(offset + len(rows)) if more else None)


# --- changing --------------------------------------------------------------------------

def _read(path: str) -> str | None:
    f = config.VAULT_ROOT / path
    return f.read_text("utf-8") if f.is_file() else None


def _patched(text: str, patch: FrontmatterPatch) -> str:
    meta, body = split_frontmatter(text)
    for k, v in patch.set.items():
        meta[k] = dt.date.fromisoformat(v) if k in DATE_KEYS and isinstance(v, str) else v
    for k in patch.unset:
        meta.pop(k, None)
    return render(meta, body)


def _pages() -> list[str]:
    root = config.VAULT_ROOT
    return [str(f.relative_to(root)) for folder in (*PAGE_DIRS, "views", "")
            for f in sorted((root / folder).glob("*.md"))]


def _plan(changes: list[Change]) -> tuple[dict[str, str | None], set[str]]:
    """Apply the changes to an in-memory overlay of the vault: (new contents by path, directly changed paths)."""
    overlay: dict[str, str | None] = {}
    direct: set[str] = set()

    def current(p: str) -> str | None:
        return overlay[p] if p in overlay else _read(p)

    for c in changes:
        path = rules.normalise_path(c.path)
        if sum(x is not None for x in (c.content, c.frontmatter, c.move_to)) != 1:
            raise ToolError(f"`{path}`: give exactly one of content, frontmatter or move_to")
        now = current(path)
        if c.base_rev is not None:
            actual = repo.rev_of(now.encode()) if now is not None else None
            if (c.base_rev == "new") != (now is None) or (c.base_rev != "new" and actual != c.base_rev):
                raise ToolError(f"`{path}` changed since you read it (rev now {actual or 'absent'}); "
                                "fetch it again and redo your change")
        if c.content is not None:
            overlay[path] = c.content.rstrip() + "\n"
            direct.add(path)
        elif c.frontmatter is not None:
            if now is None:
                raise ToolError(f"no page `{path}` to patch")
            overlay[path] = _patched(now, c.frontmatter)
            direct.add(path)
        else:
            dest = rules.normalise_path(c.move_to)
            if now is None:
                raise ToolError(f"no page `{path}` to move")
            if current(dest) is not None:
                raise ToolError(f"`{dest}` already exists")
            overlay[dest], overlay[path] = now, None
            direct.add(dest)
            old, new = path.removesuffix(".md"), dest.removesuffix(".md")
            pattern = re.compile(r"\[\[" + re.escape(old) + r"(?=[\]|#])")
            for p in dict.fromkeys([*_pages(), *overlay]):
                text = current(p)
                if text and pattern.search(text):
                    overlay[p] = pattern.sub(f"[[{new}", text)
    return overlay, direct


def _replay(key: str | None) -> WriteResult | None:
    found = repo.commit_for_key(key) if key else None
    if found is None:
        return None
    sha, paths = found
    return WriteResult(commit=sha, revs={p: repo.rev_of(t.encode()) if (t := _read(p)) is not None else None
                                         for p in paths}, replayed=True)


@vault.tool(annotations=_annotations("Create, edit or move pages", read_only=False, idempotent=False))
def write(changes: list[Change], message: str, ctx: Context, idempotency_key: str | None = None) -> WriteResult:
    """Create, replace, patch or move pages as ONE commit, e.g. a whole ingest (source + facts + entity
    + links). Each change has a path and exactly one of: content (the whole file, frontmatter + body),
    frontmatter ({"set": {"status": "confirmed", "reviewed": "2026-10-13"}, "unset": [...]}), or
    move_to (links to the page are rewritten across the vault). Pass base_rev from fetch/sql so you
    never overwrite someone else's edit ("new" = must not exist yet). All-or-nothing: everything is
    validated before anything is written. With idempotency_key, a retried call is applied only once.
    message: what and why, e.g. "ingest: SNU timetable". Conventions: fetch("AGENTS")."""
    if not changes:
        raise ToolError("no changes")
    name, who = author(ctx)
    schema = yaml.safe_load((config.VAULT_ROOT / ".schema/notes.yaml").read_text())
    with repo.locked():
        if replay := _replay(idempotency_key):
            return replay
        overlay, direct = _plan(changes)
        for path in sorted(direct):
            rules.check_write(path, overlay[path], _read(path), schema)
        for path, text in overlay.items():
            f = config.VAULT_ROOT / path
            if text is None:
                f.unlink(missing_ok=True)
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(text, "utf-8")
        sha = repo.commit(sorted(overlay), f"{message} ({who})", name, idempotency_key)
    index.reindex(sorted(overlay))
    return WriteResult(commit=sha, revs={p: repo.rev_of(t.encode()) if t is not None else None
                                         for p, t in overlay.items()})


@vault.tool(annotations=_annotations("Delete pages or unused files", read_only=False, destructive=True))
def delete(paths: list[str], message: str, ctx: Context, idempotency_key: str | None = None) -> WriteResult:
    """Delete pages that are wrong or redundant, in one commit (history keeps them: fetch("x@<commit>")
    and write them back to restore). Deleting a file note keeps its file; a file no note refers to
    any more (problems kind 'blob') can be deleted by its path, blobs/<hash>.<ext>, and so can a
    Syncthing conflict copy (problems kind 'sync-conflict') once merged; those two are gone for good."""
    if not paths:
        raise ToolError("no paths")
    name, who = author(ctx)
    with repo.locked():
        if replay := _replay(idempotency_key):
            return replay
        pages, blobs = [], []
        for raw in paths:
            if ".sync-conflict-" in raw:
                copy = (config.VAULT_ROOT / raw.strip().lstrip("/")).resolve()
                root = config.VAULT_ROOT.resolve()
                if root not in copy.parents or ".git" in copy.relative_to(root).parts or not copy.is_file():
                    raise ToolError(f"no conflict copy `{raw}`")
                blobs.append(str(copy.relative_to(root)))
                continue
            if raw.strip().lstrip("/").startswith("blobs/"):
                blob = raw.strip().lstrip("/")
                if not signing.BLOB.fullmatch(blob.removeprefix("blobs/")) or not (config.VAULT_ROOT / blob).is_file():
                    raise ToolError(f"no file `{blob}`")
                if note := next((p for p in _pages() if p.startswith("files/") and
                                 split_frontmatter(_read(p) or "")[0].get("blob") == blob), None):
                    raise ToolError(f"`{blob}` is described by {note.removesuffix('.md')}; delete that note first")
                blobs.append(blob)
                continue
            path = rules.normalise_path(raw)
            old = _read(path)
            if old is None:
                raise ToolError(f"cannot delete `{path}`: it does not exist")
            rules.check_write(path, None, old, {})
            pages.append(path)
        for path in pages:
            (config.VAULT_ROOT / path).unlink()
        sha = repo.commit(pages, f"{message} ({who})", name, idempotency_key) if pages else "none (not in git)"
        for blob in blobs:
            (config.VAULT_ROOT / blob).unlink()
    index.reindex(pages)
    return WriteResult(commit=sha, revs={p: None for p in pages + blobs})


# --- files -----------------------------------------------------------------------------

@vault.tool(annotations=_annotations("Add files to the vault", read_only=False, idempotent=False, open_world=True),
            meta={"openai/fileParams": ["file"]})
async def upload(ctx: Context, purpose: str = "", tags: list[str] | None = None, url: str | None = None,
                 file: FileRef | None = None, filename: str = "file.pdf") -> UploadResult:
    """Bring a file into the vault; it becomes a file note (files/<name>) that you then describe, tag and
    link. File bytes never go through this call:
    - a public URL: pass url; the server downloads and files it, and returns the note.
    - a file attached in ChatGPT: pass it as file; same.
    - a file in your sandbox or on your machine: call with filename only, then run the returned curl
      (or PUT to put_url with {filename} replaced). The response of each PUT names the note. One link
      takes many files for an hour; it needs network access to this server.
    purpose and tags go into the file note. Already-stored content is recognised (duplicate)."""
    who = client_name(ctx)
    await anyio.to_thread.run_sync(incoming.check_quota)
    if file is not None or url:
        source = file.download_url if file is not None else url
        part, name = await anyio.to_thread.run_sync(incoming.fetch, source, file.file_name if file else None)
        result = await anyio.to_thread.run_sync(partial(
            filing.file, part, original_name=name, origin=filing.origin_of(who) if file else "web",
            added_by=who, purpose=purpose, url=None if file else url, tags=tags))
        return UploadResult(note=result["note"], duplicate=result["duplicate"])
    ticket = incoming.open_ticket(who, purpose, tags)
    template, expires = signing.upload_link(ticket)
    target = template.replace("{filename}", urllib.parse.quote(documents.safe_name(filename), safe=""))
    return UploadResult(put_url=template, curl=f"curl -fsS -T {shlex.quote(filename)} {shlex.quote(target)}",
                        expires_at=_iso(expires))


# --- context ---------------------------------------------------------------------------

@vault.resource("vault://AGENTS", mime_type="text/markdown")
def agents() -> str:
    """How agents work in this vault: conventions, page kinds, linking."""
    return (config.VAULT_ROOT / "AGENTS.md").read_text("utf-8")


@vault.resource("vault://schema", mime_type="text/yaml")
def schema() -> str:
    """Frontmatter schema per folder and the category taxonomy."""
    return (config.VAULT_ROOT / ".schema/notes.yaml").read_text("utf-8")
