"""Read-side model of the vault: pages, frontmatter and the link graph."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

WIKILINK = re.compile(r"(!?)\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
LINK_FIELDS = ("source", "about", "related", "supersedes", "projects")
PAGE_DIRS = ("notes", "sources", "projects")


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


def as_date(value) -> dt.date | None:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


class Vault:
    def __init__(self, root: Path):
        self.root = root
        self.pages: dict[str, Page] = {}
        self.load()

    def load(self) -> None:
        self.pages.clear()
        for d in PAGE_DIRS:
            for f in sorted((self.root / d).glob("*.md")):
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
