"""The tools. Two primitives, `sql` to read and `write` to change rows; the rest are conveniences:
`search` and `fetch` are the two queries every client needs, `upload` moves file bytes.

The vault is one SQLite database (store.py). Everything an agent can know or do is a query.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import shlex
import urllib.parse
from functools import partial
from typing import Any

import anyio
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp import types as mt

from .. import config, documents, filing, incoming, markdown, pages, repo, signing, store
from .auth import author, client_name
from .models import Document, FileRef, SearchResult, SearchResults, SqlResult, Statement, UploadResult, WriteResult

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
    return f"{config.NOTES_BASE_URL}/{urllib.parse.quote(item_id.removeprefix('doc:').split('#')[0])}"


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).isoformat(timespec="seconds")


# --- the two primitives ----------------------------------------------------------------

@vault.tool(annotations=_annotations("Query the vault with SQL", read_only=True))
def sql(sql: str, params: list[Any] | dict[str, Any] | None = None, limit: int = 100,
        cursor: str | None = None) -> SqlResult:
    """Read anything: one SELECT/WITH over the vault database (SQLite), 5 s, rows as objects, ? params.
    pages(id, title, body, private, updated); facts(id, subject=page id, claim, valid_from, valid_until,
    confirmed, source, note) and the view current_facts; questions(id, question, about, asked, asked_by,
    answer, answered); files(id, blob, sha256, mime, size, name, versions); queries(name, sql, about): saved
    queries, run their sql; log(time, actor, message, changed, sha); links(src, dst); problems(kind, id, detail);
    documents(id 'doc:<path>', path, folder, name, media, mime, size); chunks(ref, page, title, text,
    embedding): every paragraph, fact and document passage, with fts(title, text) over the same rowids.
    Functions: embed(text) and cosine(a, b) for meaning, link(blob_or_doc_id) for a download URL.
    E.g. SELECT claim FROM current_facts WHERE subject = 'notes/seoul' |
    SELECT ref, text, cosine(embedding, embed('where do I live')) AS score FROM chunks ORDER BY score DESC
    LIMIT 5 | SELECT c.ref, c.text FROM fts JOIN chunks c ON c.id = fts.rowid WHERE fts MATCH 'thesis'
    ORDER BY bm25(fts) LIMIT 5 | SELECT * FROM problems. Long cells are clipped."""
    offset = store.decode_cursor(cursor)
    columns, rows, more = store.run_sql(sql, params, max(1, min(limit, 500)), offset)
    return SqlResult(columns=columns, rows=rows, truncated=more,
                     next_cursor=store.encode_cursor(offset + len(rows)) if more else None)


@vault.tool(annotations=_annotations("Change rows", read_only=False, destructive=True, idempotent=False))
def write(statements: list[Statement], message: str, ctx: Context, idempotency_key: str | None = None) -> WriteResult:
    """Change the vault: INSERT, UPDATE and DELETE statements on pages, facts, questions and queries, run as
    ONE transaction (all or nothing), logged and committed to git as one change. Examples:
    INSERT INTO pages (id, title, body) VALUES ('notes/acme', 'Acme', 'A company in Munich. See [[notes/lio]].')
    INSERT INTO facts (subject, claim, valid_from, source) VALUES ('notes/acme', 'I work at Acme.', '2026-01-01',
      '[[sources/chat-2026-01-02]]')        -- unreviewed until I confirm it
    UPDATE facts SET confirmed = date('now') WHERE id = 12            -- I confirmed it
    UPDATE facts SET valid_until = '2026-11-30' WHERE id = 12         -- it stopped being true: never delete it
    UPDATE pages SET body = ? WHERE id = 'notes/acme' AND updated = ? -- only if nobody changed it since I read it
    UPDATE pages SET id = 'notes/acme-gmbh' WHERE id = 'notes/acme'   -- rename; links and facts follow
    INSERT INTO questions (question, about, asked_by) VALUES ('Does X still hold?', 'notes/acme', 'claude')
    DELETE FROM pages WHERE id = 'notes/acme'                         -- its facts go with it; git keeps history
    DELETE FROM blobs WHERE name = '0123456789abcdef.pdf'             -- remove an unused stored file, for good
    Constraints explain themselves when violated. A statement changing more than 20 rows needs many=true.
    message: what and why, e.g. "ingest: SNU timetable". Conventions: fetch("AGENTS")."""
    if not statements:
        raise ToolError("no statements")
    name, who = author(ctx)
    result = store.apply([(s.sql, s.params, s.many) for s in statements], who, message, idempotency_key, name)
    return WriteResult(**result)


# --- the two queries every client needs -------------------------------------------------

@vault.tool(annotations=_annotations("Search the vault", read_only=True))
def search(query: str, limit: int = 10, cursor: str | None = None) -> SearchResults:
    """Search by keywords and by meaning at once, over page paragraphs, single facts, file contents and my
    documents. A hit is a page (notes/x), one fact (notes/x#fact-12: the claim with its validity period) or
    a document (doc:<path>). Then fetch(id). For exact filters, dates, counts or your own ranking use `sql`."""
    offset = store.decode_cursor(cursor)
    with store.connect() as conn:
        rows, more = store.search(conn, query, max(1, min(limit, 50)), offset)
    return SearchResults(
        results=[SearchResult(id=r["id"], title=r["title"] or r["id"], text=" ".join(r["text"].split())[:700],
                              url=_url(r["id"])) for r in rows],
        next_cursor=store.encode_cursor(offset + len(rows)) if more else None)


def _closest(wanted: str) -> str:
    with store.connect() as conn:
        return ", ".join(store.suggest(conn, wanted)) or "none"


def _page(text: str, offset: int) -> tuple[str, dict]:
    end = offset + TEXT_PAGE
    return text[offset:end], ({"next_offset": end} if end < len(text) else {})


@vault.tool(annotations=_annotations("Read a page, file or document", read_only=True))
def fetch(id: str, offset: int = 0) -> Document:
    """Full text of anything search or sql returned: a page with its facts (notes/x, AGENTS, index), the
    question queue (questions), a document (doc:University/x.pdf), or an old version (notes/x@<commit>, the
    commit from `log.sha`). File pages include the extracted text of their file and a download_url; HTML
    file pages also a view_url, which is their url. Long texts continue at metadata.next_offset."""
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
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]*", path) or not re.fullmatch(r"[0-9a-fA-F]{4,40}", commit):
            raise ToolError(f"`{path}` is not a page id")
        body, more = _page(repo.show(commit, f"{path}.md"), offset)
        return Document(id=item_id, title=path, text=body, url=_url(path), metadata={"commit": commit, **more})
    item_id = item_id.split("#", 1)[0].removesuffix(".md")  # a fact hit (notes/x#fact-3) reads its page
    with store.connect() as conn:
        if item_id in ("questions", "queries"):
            markdown.export_table(conn, item_id)
            body, more = _page((config.VAULT_ROOT / f"{item_id}.md").read_text("utf-8"), offset)
            return Document(id=item_id, title=item_id.title(), text=body, url=_url(item_id), metadata=more)
        row = conn.execute("SELECT title, private, updated FROM pages WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ToolError(f"no page `{item_id}`; closest: {_closest(item_id)}")
        text = markdown.page_text(conn, item_id)
        meta: dict[str, Any] = {"updated": row["updated"], "private": bool(row["private"])}
        meta["backlinks"] = [r[0] for r in conn.execute(
            "SELECT DISTINCT src FROM links WHERE dst = ? ORDER BY src LIMIT 50", (item_id,))]
        file = conn.execute("SELECT * FROM files WHERE id = ?", (item_id,)).fetchone()
    if file and (blob := config.VAULT_ROOT / file["blob"]).exists():
        meta["download_url"] = signing.blob_link(file["blob"], file["name"] or blob.name)
        meta["versions"] = json.loads(file["versions"])
        if file["mime"] == "text/html" and (view := pages.url(item_id)):
            meta["view_url"] = view
        if extracted := documents.text_for(blob, file["sha256"]):
            text += "\n\n## Extracted text\n\n" + extracted
    body, more = _page(text, offset)
    return Document(id=item_id, title=row["title"], text=body, url=meta.get("view_url") or _url(item_id),
                    metadata={**meta, **more})


# --- files -----------------------------------------------------------------------------

@vault.tool(annotations=_annotations("Add files to the vault", read_only=False, idempotent=False, open_world=True),
            meta={"openai/fileParams": ["file"]})
async def upload(ctx: Context, purpose: str = "", tags: list[str] | None = None, url: str | None = None,
                 file: FileRef | None = None, filename: str = "file.pdf", replaces: str | None = None) -> UploadResult:
    """Bring a file into the vault; it becomes a file page (files/<name>) whose body you then describe and
    link. File bytes never go through this call:
    - a public URL: pass url; the server downloads and files it, and returns the page.
    - a file attached in ChatGPT: pass it as file; same.
    - a file in your sandbox or on your machine: call with filename only, then run the returned curl
      (or PUT to put_url with {filename} replaced). The response of each PUT names the page. One link
      takes many files for an hour; it needs network access to this server.
    purpose becomes the first line of the page. Already-stored content is recognised (duplicate).
    replaces="files/<name>": the file becomes the new version of that page instead (same page, same
    view_url; the earlier blob is kept in files.versions). An HTML file is a page a person can open: the
    result has its view_url. Conventions for such pages: fetch("AGENTS")."""
    who = client_name(ctx)
    if replaces is not None:
        replaces = replaces.strip().lstrip("/").removesuffix(".md")
        with store.connect() as conn:
            if not conn.execute("SELECT 1 FROM files WHERE id = ?", (replaces,)).fetchone():
                raise ToolError(f"`{replaces}` is not a file page; replaces takes files/<name>")
    await anyio.to_thread.run_sync(incoming.check_quota)
    if file is not None or url:
        source = file.download_url if file is not None else url
        part, name = await anyio.to_thread.run_sync(incoming.fetch, source, file.file_name if file else None)
        result = await anyio.to_thread.run_sync(partial(
            filing.file, part, original_name=name, origin=filing.origin_of(who) if file else "web",
            added_by=who, purpose=purpose, url=None if file else url, tags=tags, replaces=replaces))
        return UploadResult(note=result["note"], duplicate=result["duplicate"], view_url=pages.view_url(result["note"]))
    ticket = incoming.open_ticket(who, purpose, tags, replaces)
    template, expires = signing.upload_link(ticket)
    target = template.replace("{filename}", urllib.parse.quote(documents.safe_name(filename), safe=""))
    return UploadResult(put_url=template, curl=f"curl -fsS -T {shlex.quote(filename)} {shlex.quote(target)}",
                        expires_at=_iso(expires))


# --- context ---------------------------------------------------------------------------

@vault.resource("vault://AGENTS", mime_type="text/markdown")
def agents() -> str:
    """How agents work in this vault: the tables, the conventions, the saved queries."""
    with store.connect() as conn:
        return markdown.page_text(conn, "AGENTS") or "The AGENTS page is missing."


@vault.resource("vault://schema", mime_type="text/plain")
def schema() -> str:
    """The database schema: tables, constraints, views and triggers."""
    return store.DDL
