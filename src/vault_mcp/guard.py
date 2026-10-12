"""Write rules from AGENTS.md, enforced in code so every agent gets them."""
from __future__ import annotations

import re

import yaml
from fastmcp.exceptions import ToolError

from .lint import schema_errors
from .vault import Page, split_frontmatter

WRITABLE = ("notes/", "projects/", "sources/")
OWNER_ONLY = ("reviewed",)

# Deliberately conservative: false positives just make the agent rephrase.
SECRET_PATTERNS = [
    (re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b"), "an IBAN"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "a card or account number"),
    (re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*\S{6,}"), "a credential"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,})\b"), "an API key"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "a private key"),
]


class Refused(ToolError):
    pass


def luhn_ok(digits: str) -> bool:
    nums = [int(d) for d in digits][::-1]
    total = sum(n if i % 2 == 0 else (n * 2 - 9 if n * 2 > 9 else n * 2) for i, n in enumerate(nums))
    return total % 10 == 0


def check_path(path: str) -> str:
    path = path.strip().removesuffix(".md").lstrip("/")
    if not path.startswith(WRITABLE) or path.count("/") != 1:
        raise Refused(f"`{path}`: agents may only write notes/<name>, projects/<name> or sources/<name>")
    if not re.fullmatch(r"[a-z]+/[a-z0-9][a-z0-9-]*", path):
        raise Refused(f"`{path}`: use lowercase-hyphenated names, e.g. notes/my-note")
    return path


def check_secrets(text: str) -> None:
    for pattern, what in SECRET_PATTERNS:
        for m in pattern.finditer(text):
            if what == "a card or account number" and not luhn_ok(re.sub(r"\D", "", m.group(0))):
                continue
            raise Refused(f"content looks like {what}; never store credentials or account numbers in the vault")


def check_write(path: str, new_text: str, old_text: str | None, schema: dict) -> None:
    """Raise Refused if writing new_text to path breaks the vault rules."""
    if path.startswith("sources/") and old_text is not None:
        raise Refused("sources are immutable: create a new source instead of editing one")
    check_secrets(new_text)
    meta, body = split_frontmatter(new_text)
    if not meta:
        raise Refused("missing YAML frontmatter; see AGENTS.md")
    old_meta = split_frontmatter(old_text)[0] if old_text else {}
    if meta.get("status") == "confirmed" and old_meta.get("status") != "confirmed":
        raise Refused("only the owner sets status: confirmed; use unreviewed")
    for key in OWNER_ONLY:
        if str(meta.get(key)) != str(old_meta.get(key)):
            raise Refused(f"only the owner sets `{key}`")
    errors = schema_errors(Page(path, meta, body), schema)
    if errors:
        raise Refused("frontmatter violates .schema/notes.yaml: " + "; ".join(errors))


def render(meta: dict, body: str) -> str:
    fm = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, width=1000)
    return f"---\n{fm}---\n{body.rstrip()}\n"
