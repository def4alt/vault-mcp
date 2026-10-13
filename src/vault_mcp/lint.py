"""Vault lint: the checks from AGENTS.md, as a report.

    vault-lint <vault-root>

The worker keeps the same findings in the index's `problems` table for agents.
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from collections import Counter
from pathlib import Path
from typing import NamedTuple

import yaml

from .vault import Page, Vault, as_date

STALE_DAYS = 90
# Capitalised words that are sentence furniture, not names.
STOPWORDS = {"I", "My", "The", "A", "An", "In", "At", "On", "For", "And", "Or", "Of", "To", "As", "Is", "It",
             "From", "With", "Not"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _as_list(value) -> list:
    return value if isinstance(value, list) else [value]


def schema_errors(page: Page, schema: dict) -> list[str]:
    if page.meta.get("__invalid__"):
        return ["frontmatter is not valid YAML"]
    section = page.path.split("/")[0]
    spec = schema[section]
    errors = [f"missing `{k}`" for k in spec["required"] if page.meta.get(k) in (None, "", [])]
    for key, value in page.meta.items():
        rule = spec["fields"].get(key)
        if rule is None:
            errors.append(f"unknown field `{key}`")
            continue
        if value in (None, "", []):
            continue
        if "enum" in rule and value not in rule["enum"]:
            errors.append(f"`{key}: {value}` not in {rule['enum']}")
        if rule.get("only_for") and page.kind not in rule["only_for"]:
            errors.append(f"`{key}` only allowed for kind {rule['only_for']}")
        kind = rule.get("type")
        if kind == "date" and as_date(value) is None:
            errors.append(f"`{key}` is not a YYYY-MM-DD date")
        lo, hi = rule.get("min", float("-inf")), rule.get("max", float("inf"))
        if kind == "int" and not (isinstance(value, int) and lo <= value <= hi):
            errors.append(f"`{key}` must be an integer" + (f" {lo}-{hi}" if "min" in rule else ""))
        if kind == "list":
            if not isinstance(value, list):
                errors.append(f"`{key}` must be a list")
                continue
            if rule.get("items") == "category":
                errors += [f"unknown category `{c}`" for c in value if c not in schema["categories"]]
            if rule.get("items") == "link":
                for v in value:
                    if not re.fullmatch(r"\[\[[^\]]+\]\]", str(v)):
                        errors.append(f"`{key}` item `{v}` is not a wikilink")
                    elif rule.get("prefix") and not str(v)[2:].startswith(tuple(_as_list(rule["prefix"]))):
                        errors.append(f"`{key}` item `{v}` must point into {rule['prefix']}")
    if section == "notes" and page.kind == "fact":
        errors += [f"fact missing `{k}`" for k in ("status", "as_of") if not page.meta.get(k)]
    return errors


class Problem(NamedTuple):
    # broken-link, conflict, superseded, stale, orphan, undistilled, missing-entity, schema, blob, sync-conflict
    kind: str
    id: str  # the page (or blob, or entity name) the problem is about
    detail: str


def lint(vault: Vault, today: dt.date | None = None) -> list[Problem]:
    today = today or dt.date.today()
    schema = yaml.safe_load((vault.root / ".schema/notes.yaml").read_text())
    notes = [p for p in vault.pages.values() if p.path.startswith("notes/")]
    superseded = vault.superseded_by()
    backlinked = {t for p in vault.pages.values() for t in p.links if t != p.path}
    entity_names = set()
    for p in notes:
        if p.kind == "entity":
            words = re.findall(r"[A-Za-z0-9]+", p.title)
            aliases = p.meta.get("aliases") or []
            entity_names |= {p.path.split("/")[1], slug(p.title), slug("".join(w[0] for w in words))}
            entity_names |= {slug(str(a)) for a in aliases}
    out: list[Problem] = []

    for p in vault.pages.values():
        for t in sorted(p.links | p.embeds):
            if not vault.exists(t):
                out.append(Problem("broken-link", p.path, f"links to `{t}`, which does not exist"))
        out += [Problem("schema", p.path, e) for e in schema_errors(p, schema)]

    for p in notes:
        status = p.meta.get("status")
        if status == "needs-update":
            out.append(Problem("conflict", p.path, str(p.meta.get("summary", ""))))
        newer = superseded.get(p.path, [])
        if newer and status not in ("outdated", "historical"):
            out.append(Problem("superseded", p.path, f"status {status}, superseded by {', '.join(newer)}"))
        if p.kind == "fact" and status == "confirmed":
            reviewed = as_date(p.meta.get("reviewed"))
            if reviewed is None or (today - reviewed).days > STALE_DAYS:
                out.append(Problem("stale", p.path, f"confirmed, last reviewed {reviewed or 'never'}"))
        linked_out = p.field_links("about") or p.field_links("related")
        if p.kind != "index" and not linked_out and p.path not in backlinked:
            out.append(Problem("orphan", p.path, "nothing links here and it links to nothing via about/related"))

    for p in vault.pages.values():
        if p.path.startswith(("sources/", "files/")) and p.path not in backlinked:
            out.append(Problem("undistilled", p.path, "no note cites it yet"))

    referenced = set()
    for p in vault.pages.values():
        if p.path.startswith("files/") and p.meta.get("blob"):
            referenced.add(str(p.meta["blob"]))
            if not (vault.root / str(p.meta["blob"])).exists():
                out.append(Problem("blob", p.path, f"its blob `{p.meta['blob']}` is missing"))
    blob_dir = vault.root / "blobs"
    for f in sorted(blob_dir.iterdir()) if blob_dir.is_dir() else []:
        if not f.name.startswith(".") and f"blobs/{f.name}" not in referenced:
            out.append(Problem("blob", f"blobs/{f.name}", "no file note describes it (write one, or delete the blob)"))

    for f in sorted(vault.root.rglob("*.sync-conflict-*")):
        if ".git" not in f.parts:
            rel = str(f.relative_to(vault.root))
            out.append(Problem("sync-conflict", rel, "Syncthing conflict copy: merge it into the original, delete it"))

    names: Counter[str] = Counter()
    for p in notes:
        if p.kind == "fact":
            names.update({w for w in re.findall(r"\b[A-Z][\w-]+(?: [A-Z][\w-]+)*", p.title) if w not in STOPWORDS})
    for name, n in names.most_common():
        if n >= 3 and slug(name) not in entity_names:
            out.append(Problem("missing-entity", name, f"named in {n} facts but has no entity note"))
    return out


def render(problems: list[Problem], today: dt.date) -> str:
    lines = [f"# Lint {today}: {len(problems)} problems", ""]
    for kind in dict.fromkeys(p.kind for p in problems):
        group = [p for p in problems if p.kind == kind]
        lines.append(f"## {kind} ({len(group)})")
        lines += [f"- {p.id}: {p.detail}" for p in group]
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    args = ap.parse_args()
    today = dt.date.today()
    print(render(lint(Vault(args.root), today), today))


if __name__ == "__main__":
    main()
