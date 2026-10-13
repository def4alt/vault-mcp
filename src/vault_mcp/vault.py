"""The vault as data: pages, frontmatter, links and naming. No I/O beyond reading files."""
from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

WIKILINK = re.compile(r"(!?)\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
LINK_FIELDS = ("source", "about", "related", "supersedes", "projects")
PAGE_DIRS = ("notes", "sources", "projects")
DATE_KEYS = ("as_of", "reviewed", "review")
FIELD_ORDER = ["kind", "status", "as_of", "reviewed", "categories", "origin", "source_kind", "file", "url",
               "source", "about", "related", "supersedes", "projects", "rating", "area", "review",
               "tags", "aliases", "summary"]


@dataclass
class Page:
    path: str  # vault-relative, without .md: "notes/dashpoint"
    meta: dict
    body: str
    links: set[str] = field(default_factory=set)  # outgoing page links (frontmatter + body)
    embeds: set[str] = field(default_factory=set)

    @property
    def kind(self) -> str | None:
        return self.meta.get("kind")

    @property
    def title(self) -> str:
        m = re.search(r"^# (.+)$", self.body, re.M)
        return m.group(1).strip() if m else self.path.rsplit("/", 1)[-1]

    def field_links(self, name: str) -> list[str]:
        values = self.meta.get(name) or []
        values = values if isinstance(values, list) else [values]
        return [t for v in values for t in link_targets(str(v))]


def link_targets(text: str) -> list[str]:
    return [m.group(2).strip() for m in WIKILINK.finditer(text) if not m.group(1)]


def split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end == -1:
        return {}, text
    try:
        meta = yaml.safe_load(text[4:end]) or {}
    except yaml.YAMLError:
        return {"__invalid__": True}, text[end + 4:].lstrip("\n")
    return (meta if isinstance(meta, dict) else {}), text[end + 4:].lstrip("\n")


def render(meta: dict, body: str) -> str:
    """Frontmatter + body, with fields in a stable order and dates as real YAML dates."""
    meta = dict(meta)
    for key in DATE_KEYS:
        if isinstance(meta.get(key), str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta[key]):
            meta[key] = dt.date.fromisoformat(meta[key])
    ordered = {k: meta[k] for k in FIELD_ORDER if meta.get(k) not in (None, "", [])}
    ordered.update({k: v for k, v in meta.items() if k not in ordered and v not in (None, "", [])})
    fm = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{fm}---\n{body.rstrip()}\n"


def as_date(value) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def slugify(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().replace("'", "")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug if len(slug) <= max_len else slug[:max_len].rsplit("-", 1)[0]


def unique_path(root: Path, folder: str, base: str) -> str:
    path, n = f"{folder}/{base}", 2
    while (root / f"{path}.md").exists():
        path, n = f"{folder}/{base}-{n}", n + 1
    return path


class Vault:
    """Read-only snapshot of the pages and their link graph."""

    def __init__(self, root: Path):
        self.root = root
        self.pages: dict[str, Page] = {}
        for d in PAGE_DIRS:
            for f in sorted((root / d).glob("*.md")):
                meta, body = split_frontmatter(f.read_text(encoding="utf-8"))
                page = Page(f"{d}/{f.stem}", meta, body)
                for name in LINK_FIELDS:
                    page.links.update(page.field_links(name))
                for m in WIKILINK.finditer(body):
                    (page.embeds if m.group(1) else page.links).add(m.group(2).strip())
                self.pages[page.path] = page

    def exists(self, target: str) -> bool:
        return target in self.pages or (self.root / target).exists() or (self.root / f"{target}.md").exists()

    def backlinks(self, path: str) -> list[str]:
        return sorted(p.path for p in self.pages.values() if path in p.links and p.path != path)

    def superseded_by(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for p in self.pages.values():
            for t in p.field_links("supersedes"):
                out.setdefault(t, []).append(p.path)
        return out

    def files_noted(self) -> dict[str, str]:
        """Shared file path -> source note describing it (`file:` frontmatter)."""
        return {str(p.meta["file"]): p.path for p in self.pages.values()
                if p.path.startswith("sources/") and p.meta.get("file")}
