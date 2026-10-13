"""Knowledge tools: one search and one fetch over everything, writes as commits, history and undo."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Any, Literal

import yaml
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp import types as mt

from .. import config, documents, index, repo, rules, sharing
from ..lint import lint as run_lint
from ..lint import render as render_lint
from ..vault import DATE_KEYS, PAGE_DIRS, Vault, render, split_frontmatter
from .identity import author, files_base
from .models import Change, CommitInfo, FetchedItem, History, Hit, SearchResults, WriteResult


def _tool(title: str, *, read_only: bool, destructive: bool = False, idempotent: bool = True) -> mt.ToolAnnotations:
    return mt.ToolAnnotations(title=title, read_only_hint=read_only, destructive_hint=destructive,
                              idempotent_hint=idempotent, open_world_hint=False)


knowledge = FastMCP("vault-knowledge")
Scope = Literal["all", "vault", "notes", "sources", "files", "documents"]


def page_url(item_id: str) -> str:
    """SilverBullet page for a vault item (opens in Andrii's browser over Tailscale)."""
    return f"{config.NOTES_BASE_URL}/{urllib.parse.quote(item_id)}"


def item_url(row) -> str | None:
    if row["scope"] == "vault":
        return page_url(row["id"])
    try:
        return sharing.document_link(row["id"].removeprefix("doc:"), files_base(), config.CITATION_TTL)[0]
    except documents.DocumentError:
        return None


def _hit(row, detailed: bool) -> Hit:
    fm = json.loads(row["fm"] or "{}")
    hit = Hit(id=row["id"], title=row["title"] or row["id"], url=item_url(row), kind=row["kind"],
              snippet=" ".join((row["snippet"] or "").split())[:240] or None)
    if detailed:
        hit.status, hit.as_of, hit.size, hit.summary = row["status"], row["as_of"], row["size"], fm.get("summary")
        hit.tags = [str(t) for t in fm.get("tags") or []] or None
        hit.score = round(-row["score"], 3) if row["score"] is not None else None
    return hit


def _closest(conn, wanted: str) -> list[str]:
    index.sync_vault(conn)
    return index.suggest(conn, wanted)


@knowledge.tool(annotations=_tool("Search the vault and documents", read_only=True))
def search(query: str = "", scope: Scope = "all", filters: dict[str, Any] | None = None,
           mode: Literal["words", "phrase", "regex"] = "words",
           order_by: Literal["relevance", "modified", "as_of", "title"] | None = None,
           response_format: Literal["concise", "detailed"] = "concise",
           limit: int = 10, cursor: str | None = None) -> SearchResults:
    """Start here. Ranked full-text search over notes, sources, projects, file notes (including the
    text inside the files) and documents. With an empty query it lists by filters.

    scope: notes (notes+projects) | sources | files | documents | vault | all.
    filters: {"kind": "fact"}, {"status": "unreviewed"}, {"tags": ["slides", "snu"]}, {"categories": "careers"},
      {"links_to": "notes/dashpoint"} (backlinks), {"linked_from": "notes/x"}, {"folder": "University"},
      {"path_prefix": "University/Courses"}, {"as_of": {"after": "2026-01-01"}}, any frontmatter field, "*" = set,
      null = missing. mode: words (default, all words, prefix match), phrase, regex.
    Then fetch(id) to read. concise gives id/title/url/snippet; detailed adds status, dates, tags, score."""
    offset = index.decode_cursor(cursor)
    with index.connect() as conn:
        index.sync_vault(conn)  # a stat scan; picks up edits made in Obsidian or SilverBullet
        rows, more = index.search(conn, query, scope, filters, mode,
                                  None if order_by in (None, "relevance") else order_by,
                                  limit=max(1, min(limit, 50)), offset=offset)
    return SearchResults(results=[_hit(r, response_format == "detailed") for r in rows],
                         next_cursor=index.encode_cursor(offset + len(rows)) if more else None)


def _vault_file(item_id: str) -> Path | None:
    root = config.VAULT_ROOT.resolve()
    for candidate in (item_id, f"{item_id}.md"):
        f = (root / candidate).resolve()
        if root in f.parents and f.is_file() and not any(p.startswith(".") for p in Path(candidate).parts):
            return f
    return None


@knowledge.tool(annotations=_tool("Read a note, file or document", read_only=True))
def fetch(id: str, offset: int = 0, limit: int = 20000) -> FetchedItem:
    """Full text of an id from search: a vault page (frontmatter + body; file notes also carry the
    extracted text of their file), a document ("doc:University/x.pdf"), or AGENTS, index, log, README,
    views/<name>. Long texts are paged by characters via offset/next_offset."""
    item_id = id.strip().lstrip("/")
    with index.connect() as conn:
        meta: dict[str, Any] = {}
        if item_id.startswith("doc:"):
            p = documents.resolve(item_id.removeprefix("doc:"))
            if not p.is_file():
                raise ToolError(f"no document `{item_id}`; closest: {', '.join(_closest(conn, item_id)) or 'none'}")
            sha = documents.sha256_of(p)
            text = documents.cached_text(sha) or documents.extract_text(p) or ""
            title, url = p.name, sharing.document_link(documents.relative(p), files_base(), config.CITATION_TTL)[0]
            meta = {"media": documents.media_of(p), "size": p.stat().st_size}
            if not text:
                text = f"({meta['media']} file without extractable text; open it with the url)"
        else:
            f = _vault_file(item_id)
            if f is None:
                raise ToolError(f"no page `{item_id}`; closest: {', '.join(_closest(conn, item_id)) or 'none'}")
            item_id = str(f.relative_to(config.VAULT_ROOT.resolve())).removesuffix(".md")
            text = f.read_text("utf-8")
            fm, body = split_frontmatter(text)
            title = (re.search(r"^# (.+)$", body, re.M) or [None, item_id])[1]
            url = page_url(item_id)
            meta = {k: str(v) if isinstance(v, dt.date) else v for k, v in fm.items()}
            meta["backlinks"] = index.backlinks(conn, item_id)[:50]
            if item_id.startswith("files/") and fm.get("blob"):
                blob = config.VAULT_ROOT / str(fm["blob"])
                if blob.exists():
                    name = str(fm.get("original_name") or blob.name)
                    meta["download_url"] = sharing.blob_link(str(fm["blob"]), name, files_base())[0]
                    extracted = documents.text_for(blob, str(fm.get("sha256") or documents.sha256_of(blob)))
                    if extracted:
                        text += "\n\n## Extracted text\n\n" + extracted
    end = offset + max(1, limit)
    return FetchedItem(id=item_id, title=title, text=text[offset:end], url=url, metadata=meta,
                       next_offset=end if end < len(text) else None)


def _apply(changes: list[tuple[str, str | None]], message: str, ctx: Context | None) -> WriteResult:
    """Validate everything, then write all changes as one commit and refresh the index."""
    name, who = author(ctx)
    schema = yaml.safe_load((config.VAULT_ROOT / ".schema/notes.yaml").read_text())
    planned = []
    for raw_path, new in changes:
        path = rules.normalise_path(raw_path)
        f = config.VAULT_ROOT / path
        old = f.read_text("utf-8") if f.exists() else None
        if new is None and old is None:
            raise ToolError(f"cannot delete `{path}`: it does not exist")
        rules.check_write(path, new, old, schema)
        planned.append((path, f, new))
    with repo.locked():
        for _, f, new in planned:
            if new is None:
                f.unlink()
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(new, "utf-8")
        sha = repo.commit([p for p, _, _ in planned], f"{message} ({who})", name)
    index.reindex([p for p, _, _ in planned])
    return WriteResult(paths=[p for p, _, _ in planned], commit=sha)


def _patched(path: str, patch) -> str:
    f = _vault_file(path)
    if f is None:
        raise ToolError(f"no page `{path}` to patch")
    meta, body = split_frontmatter(f.read_text("utf-8"))
    for k, v in patch.set.items():
        meta[k] = dt.date.fromisoformat(v) if k in DATE_KEYS and isinstance(v, str) else v
    for k in patch.unset:
        meta.pop(k, None)
    return render(meta, body)


@knowledge.tool(annotations=_tool("Create or change pages", read_only=False))
def save(changes: list[Change], message: str, ctx: Context) -> WriteResult:
    """Create, replace or patch vault pages as ONE commit, e.g. a whole ingest (source + facts +
    entity + index link). Each change gives `content` (the whole file) or `frontmatter`
    ({"set": {"status": "confirmed", "reviewed": "2026-10-13"}, "unset": [...]}). All-or-nothing:
    everything is validated first. Message like "ingest: SNU timetable"."""
    if not changes:
        raise ToolError("no changes")
    resolved = []
    for c in changes:
        if (c.content is None) == (c.frontmatter is None):
            raise ToolError(f"`{c.path}`: give exactly one of content or frontmatter")
        new = c.content.rstrip() + "\n" if c.content is not None else _patched(c.path, c.frontmatter)
        resolved.append((c.path, new))
    return _apply(resolved, message, ctx)


@knowledge.tool(annotations=_tool("Delete pages", read_only=False, destructive=True))
def delete(paths: list[str], message: str, ctx: Context) -> WriteResult:
    """Delete pages that are wrong or redundant, in one commit (history keeps them; `revert` restores).
    Deleting a file note keeps its blob; lint lists blobs without a note."""
    if not paths:
        raise ToolError("no paths")
    return _apply([(p, None) for p in paths], message, ctx)


@knowledge.tool(annotations=_tool("Rename or move a page", read_only=False, destructive=True, idempotent=False))
def move(path: str, new_path: str, message: str, ctx: Context) -> WriteResult:
    """Rename or move a page and rewrite every wikilink to it across the vault, in one commit."""
    old = rules.normalise_path(path).removesuffix(".md")
    new = rules.normalise_path(new_path).removesuffix(".md")
    src, dst = config.VAULT_ROOT / f"{old}.md", config.VAULT_ROOT / f"{new}.md"
    if not src.exists():
        raise ToolError(f"no page `{old}`")
    if dst.exists():
        raise ToolError(f"`{new}` already exists")
    link = re.compile(r"\[\[" + re.escape(old) + r"(?=[\]|#])")
    name, who = author(ctx)
    with repo.locked():
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        touched = {f"{old}.md", f"{new}.md"}
        for folder in (*PAGE_DIRS, "views", ""):
            for f in sorted((config.VAULT_ROOT / folder).glob("*.md")):
                text = f.read_text("utf-8")
                if link.search(text):
                    f.write_text(link.sub(f"[[{new}", text), "utf-8")
                    touched.add(str(f.relative_to(config.VAULT_ROOT)))
        sha = repo.commit(sorted(touched), f"{message} ({who})", name)
    index.reindex(sorted(touched))
    return WriteResult(paths=sorted(touched), commit=sha)


@knowledge.tool(annotations=_tool("Record what you did", read_only=False, idempotent=False))
def log(op: Literal["ingest", "query", "lint", "edit", "import", "share", "review"], target: str, what: str,
        ctx: Context) -> WriteResult:
    """Append one line to log.md: `- <date> <op> <target>: <what> (<you>)`. target: a wikilink like [[files/x]]."""
    name, who = author(ctx)
    line = f"- {dt.date.today()} {op} {target}: {what} ({who})"
    if "\n" in line:
        raise ToolError("one line only")
    rules.check_secrets(line)
    with repo.locked():
        with (config.VAULT_ROOT / "log.md").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        sha = repo.commit(["log.md"], f"log: {op} {target} ({who})", name)
    return WriteResult(paths=["log.md"], commit=sha)


@knowledge.tool(annotations=_tool("Show change history", read_only=True))
def history(path: str | None = None, limit: int = 20, cursor: str | None = None) -> History:
    """Recent commits for the vault or one page: who changed what, when. Use before big changes and to find
    a sha for `revert`."""
    offset = index.decode_cursor(cursor)
    limit = max(1, min(limit, 100))
    args = ["log", f"--skip={offset}", f"-{limit + 1}", "--format=%h%x1f%cI%x1f%an%x1f%s"]
    if path:
        f = _vault_file(path)
        if f is None:
            raise ToolError(f"no page `{path}`")
        args += ["--follow", "--", str(f.relative_to(config.VAULT_ROOT.resolve()))]
    lines = [line for line in repo.git(*args).splitlines() if line]
    commits = [CommitInfo(**dict(zip(("sha", "date", "author", "message"), line.split("\x1f"), strict=True)))
               for line in lines[:limit]]
    return History(commits=commits, next_cursor=index.encode_cursor(offset + limit) if len(lines) > limit else None)


@knowledge.tool(annotations=_tool("Undo a commit", read_only=False, destructive=True, idempotent=False))
def revert(commit: str, ctx: Context) -> WriteResult:
    """Undo an earlier commit with a new one (sha from `history`). Works when nothing it touched changed since."""
    if not re.fullmatch(r"[0-9a-f]{7,40}", commit):
        raise ToolError("give a commit sha from `history`")
    name, who = author(ctx)
    with repo.locked():
        changed = repo.git("show", "--name-only", "--format=", commit).split()
        try:
            repo.git("-c", f"user.name={name}", "-c", "user.email=vault-mcp@def4alt.com", "revert", "--no-edit", commit)
        except repo.GitError as e:
            repo.git("revert", "--abort")
            raise ToolError(f"cannot revert {commit} cleanly: {e}") from None
        sha = repo.git("rev-parse", "--short", "HEAD").strip()
    index.reindex(changed)
    return WriteResult(paths=changed, commit=sha)


@knowledge.tool(annotations=_tool("Check vault health", read_only=True))
def lint() -> str:
    """Health report: conflicts, stale facts, orphans, undistilled sources and files, missing entities,
    schema violations, broken links, blobs without notes. Save it with save() as notes/lint-<date>."""
    today = dt.date.today()
    return render_lint(run_lint(Vault(config.VAULT_ROOT), today), today)
