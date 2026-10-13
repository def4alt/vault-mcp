"""Vault tools: find, read and change notes; history and undo; lint."""
from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path
from typing import Any

import yaml
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from mcp import types as mt

from .. import config, repo, rules
from ..lint import lint as run_lint
from ..lint import render as render_lint
from ..vault import DATE_KEYS, Page, Vault, render, split_frontmatter
from .identity import author
from .models import Change, CommitInfo, PageInfo, WriteResult

READ = mt.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
WRITE = mt.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
CHANGE = mt.ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

notes = FastMCP("vault-notes")


def _info(p: Page) -> PageInfo:
    m = p.meta
    return PageInfo(path=p.path, title=p.title, kind=m.get("kind"), status=m.get("status"),
                    as_of=str(m["as_of"]) if m.get("as_of") else None,
                    reviewed=str(m["reviewed"]) if m.get("reviewed") else None,
                    categories=[str(c) for c in m.get("categories") or []], summary=m.get("summary"))


def _file(path: str) -> Path:
    root = config.VAULT_ROOT.resolve()
    path = path.strip().lstrip("/")
    for candidate in (path, f"{path}.md"):
        f = (root / candidate).resolve()
        if root in f.parents and f.is_file() and not any(p.startswith(".") for p in Path(candidate).parts):
            return f
    raise ToolError(f"no page `{path}`")


def _apply(changes: list[Change], message: str, ctx: Context | None) -> WriteResult:
    name, who = author(ctx)
    schema = yaml.safe_load((config.VAULT_ROOT / ".schema/notes.yaml").read_text())
    planned = []
    for c in changes:  # validate everything before touching anything
        path = rules.normalise_path(c.path)
        f = config.VAULT_ROOT / path
        old = f.read_text("utf-8") if f.exists() else None
        if c.delete and old is None:
            raise ToolError(f"cannot delete `{path}`: it does not exist")
        new = None if c.delete else (c.content or "").rstrip() + "\n"
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
    return WriteResult(paths=[p for p, _, _ in planned], action="applied", commit=sha)


@notes.tool(annotations=READ)
def search(query: str, folder: str | None = None, limit: int = 20) -> list[PageInfo]:
    """Full-text search over notes, sources and projects; title and summary matches rank first.
    folder: optionally "notes", "sources" or "projects"."""
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1]
    if not terms:
        raise ToolError("query needs at least one word")
    hits = []
    for p in Vault(config.VAULT_ROOT).pages.values():
        if folder and not p.path.startswith(folder.rstrip("/") + "/"):
            continue
        head, body = f"{p.path} {p.title} {p.meta.get('summary', '')}".lower(), p.body.lower()
        if all(t in head or t in body for t in terms):
            hits.append((sum(5 * head.count(t) + body.count(t) for t in terms), p))
    hits.sort(key=lambda h: -h[0])
    return [_info(p) for _, p in hits[:limit]]


@notes.tool(annotations=READ)
def read(path: str) -> str:
    """Read any vault file: "AGENTS", "index", "log", "README", "notes/dashpoint", "views/stale.base"."""
    return _file(path).read_text("utf-8")


@notes.tool(annotations=READ)
def query(where: dict[str, Any] | None = None, folder: str = "notes", order_by: str = "as_of",
          descending: bool = False, limit: int = 50) -> list[PageInfo]:
    """Filter pages by frontmatter. `where` maps field -> value; list fields match if they contain it;
    "*" means "is set", null means "is missing". Example: {"kind": "fact", "status": "unreviewed"}."""
    def matches(p: Page) -> bool:
        for key, want in (where or {}).items():
            have = p.meta.get(key)
            if want is None:
                ok = have in (None, "", [])
            elif want == "*":
                ok = have not in (None, "", [])
            elif isinstance(have, list):
                ok = want in have or f"[[{want}]]" in have
            else:
                ok = str(have) == str(want)
            if not ok:
                return False
        return True

    rows = [p for p in Vault(config.VAULT_ROOT).pages.values()
            if p.path.startswith(folder.rstrip("/") + "/") and matches(p)]
    rows.sort(key=lambda p: str(p.meta.get(order_by) or ""), reverse=descending)
    return [_info(p) for p in rows[:limit]]


@notes.tool(annotations=READ)
def backlinks(path: str) -> list[PageInfo]:
    """Pages linking to `path` (frontmatter or body). Walk from an entity to everything about it."""
    v = Vault(config.VAULT_ROOT)
    return [_info(v.pages[b]) for b in v.backlinks(path.strip().removesuffix(".md"))]


@notes.tool(annotations=WRITE)
def write_note(path: str, content: str, message: str, ctx: Context) -> WriteResult:
    """Create or replace one vault file (frontmatter + Markdown): notes/, sources/ (new only), projects/,
    views/, index, AGENTS... Pages in notes/, sources/ and projects/ are checked against
    .schema/notes.yaml. Committed with `message`."""
    return _apply([Change(path=path, content=content)], message, ctx)


@notes.tool(annotations=CHANGE)
def apply_changes(changes: list[Change], message: str, ctx: Context) -> WriteResult:
    """Several creates, replacements and deletions as ONE commit: a whole ingest (source + facts +
    entity + index link) or a merge of duplicates. All-or-nothing: everything is validated first."""
    if not changes:
        raise ToolError("no changes")
    return _apply(changes, message, ctx)


@notes.tool(annotations=WRITE)
def update_frontmatter(path: str, message: str, ctx: Context, fields: dict[str, Any] | None = None,
                       unset: list[str] | None = None) -> WriteResult:
    """Change frontmatter fields without touching the body,
    e.g. fields={"status": "outdated"} on a note that a newer note supersedes."""
    f = _file(path)
    meta, body = split_frontmatter(f.read_text("utf-8"))
    for k, v in (fields or {}).items():
        meta[k] = dt.date.fromisoformat(v) if k in DATE_KEYS and isinstance(v, str) else v
    for k in unset or []:
        meta.pop(k, None)
    rel = str(f.relative_to(config.VAULT_ROOT.resolve()))
    return _apply([Change(path=rel, content=render(meta, body))], message, ctx)


@notes.tool(annotations=CHANGE)
def move_note(path: str, new_path: str, message: str, ctx: Context) -> WriteResult:
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
        for f in config.VAULT_ROOT.rglob("*.md"):
            rel = f.relative_to(config.VAULT_ROOT)
            if any(part.startswith(".") for part in rel.parts):
                continue
            text = f.read_text("utf-8")
            if link.search(text):
                f.write_text(link.sub(f"[[{new}", text), "utf-8")
                touched.add(str(rel))
        sha = repo.commit(sorted(touched), f"{message} ({who})", name)
    return WriteResult(paths=sorted(touched), action="moved", commit=sha)


@notes.tool(annotations=WRITE)
def append_log(op: str, target: str, what: str, agent: str, ctx: Context) -> WriteResult:
    """Append one line to log.md: `- <date> <op> <target>: <what> (<agent>)`.
    op: ingest | query | lint | edit | import | share. target: a wikilink like [[sources/x]]."""
    line = f"- {dt.date.today()} {op} {target}: {what} ({agent})"
    if "\n" in line:
        raise ToolError("one line only")
    rules.check_secrets(line)
    name, who = author(ctx)
    with repo.locked():
        with (config.VAULT_ROOT / "log.md").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        sha = repo.commit(["log.md"], f"log: {op} {target} ({who})", name)
    return WriteResult(paths=["log.md"], action="logged", commit=sha)


@notes.tool(annotations=READ)
def history(path: str | None = None, limit: int = 20) -> list[CommitInfo]:
    """Recent commits, for the whole vault or one file: who changed what, and when."""
    args = ["log", f"-{min(limit, 200)}", "--format=%h%x1f%cI%x1f%an%x1f%s"]
    if path:
        args += ["--follow", "--", str(_file(path).relative_to(config.VAULT_ROOT.resolve()))]
    return [CommitInfo(**dict(zip(("sha", "date", "author", "message"), line.split("\x1f"), strict=True)))
            for line in repo.git(*args).splitlines() if line]


@notes.tool(annotations=CHANGE)
def revert(commit: str, ctx: Context) -> WriteResult:
    """Undo an earlier commit with a new commit. Pick the sha from `history`; it reverts cleanly
    only if nothing it touched has changed since."""
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
    return WriteResult(paths=changed, action=f"reverted {commit} ({who})", commit=sha)


@notes.tool(annotations=READ)
def lint() -> str:
    """Vault health report: conflicts, stale facts, orphans, undistilled sources, missing entities,
    schema violations and broken links. Save it with write_note as notes/lint-<date> if useful."""
    today = dt.date.today()
    return render_lint(run_lint(Vault(config.VAULT_ROOT), today), today)
