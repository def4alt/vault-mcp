"""The few write rules that stay hard. Everything else is guidance in AGENTS.md:
agents are trusted collaborators, and git history makes every change reversible.
"""
from __future__ import annotations

import re

from .lint import schema_errors
from .vault import PAGE_DIRS, Page, split_frontmatter

# The vault is pushed to GitHub and backed up to B2: secrets must never land in it.
SECRET_PATTERNS = [
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b"), "an IBAN"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "a card or account number"),
    (re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S{6,}"), "a credential"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}"
                r"|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "an API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
]


class RuleViolation(ValueError):
    pass


def _luhn(digits: str) -> bool:
    nums = [int(d) for d in digits][::-1]
    return sum(n if i % 2 == 0 else (n * 2 - 9 if n * 2 > 9 else n * 2) for i, n in enumerate(nums)) % 10 == 0


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


def check_secrets(text: str) -> None:
    for pattern, what in SECRET_PATTERNS:
        for m in pattern.finditer(text):
            if what == "a card or account number" and not _luhn(re.sub(r"\D", "", m.group(0))):
                continue
            raise RuleViolation(f"content looks like {what}; the vault is pushed to GitHub, keep secrets out of it")


def check_write(path: str, new_text: str | None, old_text: str | None, schema: dict) -> None:
    """Raise RuleViolation if writing new_text (None = delete) to path breaks a hard rule."""
    if path.startswith("sources/") and old_text is not None:
        raise RuleViolation("sources are the raw record and stay as captured; add a new source or a note instead")
    if new_text is None:
        return
    check_secrets(new_text)
    if path.split("/")[0] not in PAGE_DIRS or path.count("/") != 1 or not path.endswith(".md"):
        return
    meta, body = split_frontmatter(new_text)
    if not meta:
        raise RuleViolation(f"{path} needs YAML frontmatter; see .schema/notes.yaml")
    errors = schema_errors(Page(path.removesuffix(".md"), meta, body), schema)
    if errors:
        raise RuleViolation("frontmatter does not match .schema/notes.yaml: " + "; ".join(errors))
