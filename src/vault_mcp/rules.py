"""The write rules that stay hard: valid paths and schema-valid frontmatter. Everything else is
guidance in AGENTS.md: agents are trusted collaborators, and git history makes every change reversible.
Content is never inspected: no secret scanning, and sources can be corrected like any page.
"""
from __future__ import annotations

import re

from .lint import schema_errors
from .vault import PAGE_DIRS, Page, split_frontmatter


class RuleViolation(ValueError):
    pass


def normalise_path(path: str) -> str:
    """A vault path with its extension (.md unless .base is given); hidden paths are refused."""
    path = path.strip().lstrip("/")
    if not path.endswith((".md", ".base")):
        path += ".md"
    if any(p in ("", "..") or p.startswith(".") for p in path.split("/")):
        raise RuleViolation(f"`{path}`: hidden paths, `..` and .git are off limits")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9/_-]*\.(md|base)", path):
        raise RuleViolation(f"`{path}`: use letters, digits, hyphens and slashes, e.g. notes/my-note")
    return path


def check_write(path: str, new_text: str, schema: dict) -> None:
    """Raise RuleViolation if the frontmatter of a page in notes/, sources/, projects/ or files/ is invalid."""
    if path.split("/")[0] not in PAGE_DIRS or path.count("/") != 1 or not path.endswith(".md"):
        return
    meta, body = split_frontmatter(new_text)
    if not meta:
        raise RuleViolation(f"{path} needs YAML frontmatter; see .schema/notes.yaml")
    errors = schema_errors(Page(path.removesuffix(".md"), meta, body), schema)
    if errors:
        raise RuleViolation("frontmatter does not match .schema/notes.yaml: " + "; ".join(errors))
