"""Vault lint: the checks from AGENTS.md, as a report.

    vault-lint <vault-root> [--write]
"""
from __future__ import annotations

import argparse
import datetime as dt
import re
from collections import Counter
from pathlib import Path

import yaml

from .vault import Page, Vault, as_date

STALE_DAYS = 90
# Capitalised words that are sentence furniture, not names.
STOPWORDS = {"I", "My", "The", "A", "An", "In", "At", "On", "For", "And", "Or", "Of", "To", "As", "Is", "It", "From", "With", "Not"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def schema_errors(page: Page, schema: dict) -> list[str]:
    if page.meta.get("__invalid__"):
        return ["frontmatter is not valid YAML"]
    section = {"notes": "notes", "sources": "sources", "projects": "projects"}[page.path.split("/")[0]]
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
        if kind == "int" and not (isinstance(value, int) and rule["min"] <= value <= rule["max"]):
            errors.append(f"`{key}` must be an integer {rule['min']}-{rule['max']}")
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
                    elif rule.get("prefix") and not str(v)[2:].startswith(rule["prefix"]):
                        errors.append(f"`{key}` item `{v}` must point into {rule['prefix']}")
    if section == "notes" and page.kind == "fact":
        errors += [f"fact missing `{k}`" for k in ("status", "as_of") if not page.meta.get(k)]
    return errors


def lint(vault: Vault, today: dt.date | None = None) -> dict[str, list[str]]:
    today = today or dt.date.today()
    schema = yaml.safe_load((vault.root / ".schema/notes.yaml").read_text())
    notes = [p for p in vault.pages.values() if p.path.startswith("notes/")]
    superseded = vault.superseded_by()
    # Lint reports link to everything they flag; counting those links would hide the findings.
    linking = [p for p in vault.pages.values() if not p.path.startswith("notes/lint-")]
    backlinked = {t for p in linking for t in p.links if t != p.path}
    entity_names = set()
    for p in notes:
        if p.kind == "entity":
            words = re.findall(r"[A-Za-z0-9]+", p.title)
            aliases = p.meta.get("aliases") or []
            entity_names |= {p.path.split("/")[1], slug(p.title), slug("".join(w[0] for w in words))}
            entity_names |= {slug(str(a)) for a in aliases}

    r: dict[str, list[str]] = {k: [] for k in (
        "Broken links", "Conflicts", "Superseded but not outdated", "Stale confirmed facts",
        "Orphans", "Undistilled sources", "Missing entities", "Schema violations")}

    for p in vault.pages.values():
        for t in sorted(p.links | p.embeds):
            if not vault.exists(t):
                r["Broken links"].append(f"[[{p.path}]] → `{t}`")
        for e in schema_errors(p, schema):
            r["Schema violations"].append(f"[[{p.path}]]: {e}")

    for p in notes:
        status = p.meta.get("status")
        if status == "needs-update":
            r["Conflicts"].append(f"[[{p.path}]]: {p.meta.get('summary', '')}")
        newer = superseded.get(p.path, [])
        if newer and status not in ("outdated", "historical"):
            r["Superseded but not outdated"].append(
                f"[[{p.path}]] ({status}) superseded by " + ", ".join(f"[[{n}]]" for n in newer))
        if p.kind == "fact" and status == "confirmed":
            reviewed = as_date(p.meta.get("reviewed"))
            if reviewed is None or (today - reviewed).days > STALE_DAYS:
                r["Stale confirmed facts"].append(f"[[{p.path}]] (reviewed {reviewed or 'never'})")
        if (p.kind != "index" and not p.path.startswith("notes/lint-") and not p.field_links("about") and not p.field_links("related")
                and p.path not in backlinked):
            r["Orphans"].append(f"[[{p.path}]]")

    for p in vault.pages.values():
        if p.path.startswith("sources/") and p.path not in backlinked:
            r["Undistilled sources"].append(f"[[{p.path}]]")

    names: Counter[str] = Counter()
    for p in notes:
        if p.kind == "fact":
            names.update({w for w in re.findall(r"\b[A-Z][\w-]+(?: [A-Z][\w-]+)*", p.title) if w not in STOPWORDS})
    for name, n in names.most_common():
        if n >= 3 and slug(name) not in entity_names:
            r["Missing entities"].append(f"{name} ({n} facts)")
    return r


def render(report: dict[str, list[str]], today: dt.date) -> str:
    total = sum(len(v) for v in report.values())
    meta = {"kind": "reference", "as_of": today, "categories": ["systems"],
            "summary": f"Lint report {today}: {total} findings."}
    lines = ["---", yaml.safe_dump(meta, sort_keys=False).rstrip(), "---", f"# Lint {today}", ""]
    for section, items in report.items():
        lines.append(f"## {section} ({len(items)})")
        lines += [f"- {i}" for i in items] or ["- none"]
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--write", action="store_true", help="write notes/lint-YYYY-MM-DD.md")
    args = ap.parse_args()
    today = dt.date.today()
    text = render(lint(Vault(args.root), today), today)
    if args.write:
        (args.root / f"notes/lint-{today}.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
