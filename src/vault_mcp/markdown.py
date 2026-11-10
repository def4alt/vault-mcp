"""The Markdown export: every page as a file a person (or git, or SilverBullet) can read.

The database is the source of truth; this is a view of it, written after every change and committed,
so git keeps the history. It is also the way back: `vault-import` rebuilds the database from it.

    <id>.md         [frontmatter: private, file] + "# Title" + body + "## Facts" table
    questions.md    the question queue
    queries.md      the saved queries

Do not edit the files: the next export of a page overwrites them.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path

from . import config

WIKILINK = re.compile(r"!?\[\[([^\]|#\\]+)(?:#[^\]|\\]*)?(?:\\?\|[^\]]*)?\]\]")
FACT_COLUMNS = ["#", "Claim", "From", "Until", "Confirmed", "Source", "Note"]
QUESTION_COLUMNS = ["#", "Question", "About", "Asked", "By", "Answer", "Answered"]
GENERATED = {"README", "questions", "queries"}  # root files that are not pages


def link_targets(text: str) -> list[str]:
    """Targets of the [[wikilinks]] and ![[embeds]] in text."""
    text = re.sub(r"```.*?```|`[^`\n]*`", "", text, flags=re.S)  # examples in code are not links
    return [m.group(1).strip() for m in WIKILINK.finditer(text)]


def _cell(value) -> str:
    return "" if value is None else str(value).replace("|", "\\|").replace("\r", "").replace("\n", "<br>")


def _uncell(text: str) -> str | None:
    text = text.strip().replace("\\|", "|").replace("<br>", "\n")
    return text or None


def table(columns: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def read_table(text: str, columns: list[str]) -> list[list[str | None]]:
    """Rows of the first Markdown table in text whose header is `columns`."""
    lines = [ln for ln in text.splitlines() if ln.strip().startswith("|")]
    split = [[_uncell(c) for c in re.split(r"(?<!\\)\|", ln.strip().removeprefix("|").removesuffix("|"))]
             for ln in lines]
    if not split or [c or "" for c in split[0]] != columns:
        return []
    return [row for row in split[2:] if len(row) == len(columns)]


def render(page: sqlite3.Row, facts: list[sqlite3.Row], file: sqlite3.Row | None) -> str:
    """A page as Markdown: what `fetch` returns and what the export stores."""
    head = []
    if page["private"]:
        head.append("private: true")
    if file:
        head.append("file: " + json.dumps({**{k: file[k] for k in ("blob", "sha256", "mime", "size", "name")},
                                           "versions": json.loads(file["versions"])}))
    out = ("---\n" + "\n".join(head) + "\n---\n" if head else "") + f"# {page['title']}\n\n{page['body'].strip()}\n"
    if facts:
        rows = [[f["id"], f["claim"], f["valid_from"], f["valid_until"], f["confirmed"], f["source"], f["note"]]
                for f in facts]
        out += "\n## Facts\n\n" + table(FACT_COLUMNS, rows) + "\n"
    return out


def parse(text: str) -> dict:
    """The inverse of render: {"private", "file", "title", "body", "facts"}."""
    meta: dict = {}
    if text.startswith("---\n") and (end := text.find("\n---\n", 4)) != -1:
        try:
            for line in text[4:end].splitlines():
                key, _, value = line.partition(": ")
                meta[key] = json.loads(value)
            text = text[end + 5:]
        except ValueError:  # a body that happens to open with a rule, not our header
            meta = {}
    title = (re.search(r"^# (.+)$", text, re.M) or [None, ""])[1].strip()
    body = re.sub(r"^# .+\n?", "", text, count=1, flags=re.M)
    facts: list[list] = []
    if (at := body.rfind("\n## Facts\n\n| # | Claim |")) != -1:
        facts, body = read_table(body[at:], FACT_COLUMNS), body[:at]
    return {"private": bool(meta.get("private")), "file": meta.get("file"), "title": title,
            "body": body.strip(), "facts": facts}


def page_text(conn: sqlite3.Connection, pid: str) -> str | None:
    page = conn.execute("SELECT * FROM pages WHERE id = ?", (pid,)).fetchone()
    if page is None:
        return None
    facts = conn.execute("SELECT * FROM facts WHERE subject = ? ORDER BY valid_from, id", (pid,)).fetchall()
    return render(page, facts, conn.execute("SELECT * FROM files WHERE id = ?", (pid,)).fetchone())


def export_page(conn: sqlite3.Connection, pid: str) -> str:
    """Write (or remove) the export file of one page; returns its path in the repository."""
    path = f"{pid}.md"
    f = config.VAULT_ROOT / path
    text = page_text(conn, pid)
    if text is None:
        f.unlink(missing_ok=True)
    else:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, "utf-8")
    return path


def export_table(conn: sqlite3.Connection, name: str) -> str:
    """Write questions.md or queries.md; returns its path in the repository."""
    if name == "questions":
        rows = [list(r) for r in conn.execute(
            "SELECT id, question, about, asked, asked_by, answer, answered FROM questions ORDER BY id")]
        text = ("# Questions\n\nWhat agents need me to answer. Rows of the `questions` table; an empty Answer "
                "means open.\n\n" + table(QUESTION_COLUMNS, rows) + "\n")
    else:
        text = "# Queries\n\nSaved queries, rows of the `queries` table. Run one with `sql`.\n" + "".join(
            f"\n## {r['name']}\n\n{r['about'] or ''}\n\n```sql\n{r['sql'].strip()}\n```\n"
            for r in conn.execute("SELECT * FROM queries ORDER BY name"))
    (config.VAULT_ROOT / f"{name}.md").write_text(text, "utf-8")
    return f"{name}.md"


def page_files(root: Path) -> dict[str, Path]:
    """Every exported page under root: id -> file."""
    out = {}
    for f in sorted(root.rglob("*.md")):
        rel = f.relative_to(root)
        if any(p.startswith(".") for p in rel.parts) or rel.parts[0] == "blobs":
            continue
        pid = str(rel.with_suffix(""))
        if pid not in GENERATED:
            out[pid] = f
    return out


def import_all(conn: sqlite3.Connection, root: Path) -> int:
    """Load an export into an empty database (disaster recovery). Returns the number of pages."""
    if conn.execute("SELECT 1 FROM pages LIMIT 1").fetchone():
        raise SystemExit("the database already has pages; import only into an empty one")
    files = page_files(root)
    conn.execute("BEGIN IMMEDIATE")
    parsed = {pid: parse(f.read_text("utf-8")) for pid, f in files.items()}
    for pid, p in parsed.items():
        conn.execute("INSERT INTO pages (id, title, body, private) VALUES (?, ?, ?, ?)",
                     (pid, p["title"] or pid, p["body"], int(p["private"])))
        if p["file"]:
            f = p["file"]
            conn.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?)", (
                pid, f["blob"], f["sha256"], f.get("mime"), f.get("size"), f.get("name"),
                json.dumps(f.get("versions") or [])))
    for pid, p in parsed.items():
        conn.executemany("INSERT INTO facts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         [(row[0], pid, *row[1:]) for row in p["facts"]])
    q = root / "questions.md"
    if q.is_file():
        conn.executemany("INSERT INTO questions VALUES (?, ?, ?, ?, ?, ?, ?)",
                         read_table(q.read_text("utf-8"), QUESTION_COLUMNS))
    q = root / "queries.md"
    if q.is_file():
        for name, about, sql in re.findall(r"^## (.+)\n\n(.*?)\n\n```sql\n(.*?)\n```", q.read_text("utf-8"),
                                           re.M | re.S):
            conn.execute("INSERT INTO queries VALUES (?, ?, ?)", (name.strip(), sql, about.strip() or None))
    conn.execute("COMMIT")
    return len(parsed)


def main() -> None:
    from . import store
    ap = argparse.ArgumentParser(description="Rebuild the database from the Markdown export (vault-import), "
                                             "or write the whole export again (vault-import --export).")
    ap.add_argument("--export", action="store_true", help="re-derive and re-export every page instead")
    args = ap.parse_args()
    with store.connect() as conn:
        if not args.export:
            print(f"imported {import_all(conn, config.VAULT_ROOT)} pages from {config.VAULT_ROOT}")
        store.derive_all(conn)
        print(f"derived and exported {len(store.drain(conn))} files")


if __name__ == "__main__":
    main()
