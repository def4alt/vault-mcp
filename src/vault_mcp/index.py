"""A derived SQLite index over the vault and the documents tree.

Markdown and git stay the source of truth; this database can be deleted at any time
and rebuilt (`vault-index --rebuild`). It gives ranked full-text search (FTS5, BM25),
facets (tags, categories), links/backlinks and frontmatter queries.

Incremental sync compares (mtime, size) with what is indexed, so a pass over an
unchanged vault only stats files. WAL mode lets the MCP server and the worker write
from separate processes on the same host.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import re
import sqlite3
from collections.abc import Iterator
from pathlib import Path

from . import config, documents
from .vault import PAGE_DIRS, page_paths, read_page

SCHEMA_VERSION = "1"
LIST_FACETS = ("tags", "categories")
SCOPES = {
    "all": None,
    "vault": "i.scope = 'vault'",
    "notes": "i.folder IN ('notes', 'projects')",
    "sources": "i.folder = 'sources'",
    "files": "i.folder = 'files'",
    "documents": "i.scope = 'documents'",
}
ORDER = {"as_of": "i.as_of", "modified": "i.mtime", "title": "i.title", "size": "i.size"}

DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE items(
  id TEXT PRIMARY KEY, scope TEXT NOT NULL, folder TEXT, kind TEXT, status TEXT, title TEXT,
  summary TEXT, as_of TEXT, reviewed TEXT, mtime REAL, size INTEGER, sha256 TEXT, fm TEXT
);
CREATE TABLE facets(id TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE links(src TEXT NOT NULL, dst TEXT NOT NULL, field TEXT NOT NULL);
CREATE INDEX facets_lookup ON facets(field, value);
CREATE INDEX facets_id ON facets(id);
CREATE INDEX links_dst ON links(dst);
CREATE INDEX links_src ON links(src);
CREATE INDEX items_folder ON items(folder);
CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, title, summary, body, tokenize='unicode61 remove_diacritics 2');
"""


def _regexp(pattern: str, value: str | None) -> bool:
    return value is not None and re.search(pattern, value, re.I | re.M) is not None


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = config.index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.create_function("regexp", 2, _regexp, deterministic=True)
    try:
        version = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.OperationalError:
        version = None
    if not version or version[0] != SCHEMA_VERSION:
        _create(conn)
    try:
        yield conn
    finally:
        conn.close()


def _create(conn: sqlite3.Connection) -> None:
    """(Re)create the schema in one transaction; safe when two processes start at once."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
    except sqlite3.OperationalError:
        current = None
    if current and current[0] == SCHEMA_VERSION:
        conn.execute("COMMIT")
        return
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' "
                                "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'fts_%'").fetchall():
        conn.execute(f'DROP TABLE IF EXISTS "{name}"')
    for statement in filter(str.strip, DDL.split(";")):
        conn.execute(statement)
    conn.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
    conn.execute("COMMIT")


def _delete(conn: sqlite3.Connection, item_id: str) -> None:
    for table, col in (("items", "id"), ("facets", "id"), ("links", "src"), ("fts", "id")):
        conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (item_id,))


def _put(conn, item: dict, body: str, facets: list[tuple[str, str]], links: list[tuple[str, str]]) -> None:
    _delete(conn, item["id"])
    cols = ", ".join(item)
    conn.execute(f"INSERT INTO items ({cols}) VALUES ({', '.join('?' * len(item))})", list(item.values()))
    conn.execute("INSERT INTO fts (id, title, summary, body) VALUES (?, ?, ?, ?)",
                 (item["id"], item.get("title"), item.get("summary"), body))
    conn.executemany("INSERT INTO facets VALUES (?, ?, ?)", [(item["id"], f, v) for f, v in facets])
    conn.executemany("INSERT INTO links VALUES (?, ?, ?)", [(item["id"], d, f) for f, d in links])


def _str(value) -> str | None:
    return None if value in (None, "", []) else str(value)


def index_page(conn: sqlite3.Connection, path: str) -> None:
    """(Re)index one vault page, or drop it if the file is gone."""
    f = config.VAULT_ROOT / f"{path}.md"
    if not f.exists():
        _delete(conn, path)
        return
    page, st = read_page(config.VAULT_ROOT, path), f.stat()
    m = page.meta
    body = page.body
    if page.path.startswith("files/") and m.get("blob") and m.get("sha256"):
        blob = config.VAULT_ROOT / str(m["blob"])
        if blob.exists():
            body += "\n\n" + (documents.text_for(blob, str(m["sha256"])) or "")
    facets = [(field, str(v)) for field in LIST_FACETS for v in (m.get(field) or []) if isinstance(m.get(field), list)]
    facets += [(field, str(m[field])) for field in ("kind", "status", "origin", "media", "source_kind") if m.get(field)]
    links = [(field, t) for field in ("source", "about", "related", "supersedes", "projects")
             for t in page.field_links(field)]
    links += [("body", t) for t in sorted(page.links - {t for _, t in links})]
    summary = " ".join(filter(None, [_str(m.get("summary")), *(str(t) for t in m.get("tags") or [])]))
    _put(conn, {
        "id": path, "scope": "vault", "folder": path.split("/")[0], "kind": _str(m.get("kind")),
        "status": _str(m.get("status")), "title": page.title, "summary": summary,
        "as_of": _str(m.get("as_of")), "reviewed": _str(m.get("reviewed")), "mtime": st.st_mtime,
        "size": st.st_size, "sha256": _str(m.get("sha256")), "fm": json.dumps(m, default=str),
    }, body, facets, links)


def _stale(conn, scope: str, entries: dict[str, tuple[float, int]]) -> tuple[list[str], list[str]]:
    known = {r["id"]: (r["mtime"], r["size"]) for r in
             conn.execute("SELECT id, mtime, size FROM items WHERE scope = ?", (scope,))}
    changed = [i for i, stat in entries.items() if known.get(i) != stat]
    gone = [i for i in known if i not in entries]
    return changed, gone


def sync_vault(conn: sqlite3.Connection) -> int:
    entries = {}
    for path in page_paths(config.VAULT_ROOT):
        st = (config.VAULT_ROOT / f"{path}.md").stat()
        entries[path] = (st.st_mtime, st.st_size)
    changed, gone = _stale(conn, "vault", entries)
    conn.execute("BEGIN IMMEDIATE")
    for path in changed + gone:
        index_page(conn, path)
    conn.execute("COMMIT")
    return len(changed) + len(gone)


def sync_documents(conn: sqlite3.Connection, extract: bool) -> int:
    """Index the documents tree. With extract=False (MCP server) only cached text is used."""
    drop = config.DOCS_ROOT / config.DROP_REL
    entries: dict[str, tuple[float, int]] = {}
    files: dict[str, Path] = {}
    for p in documents.walk(config.DOCS_ROOT):
        if drop in p.parents:
            continue
        st = p.stat()
        rel = documents.relative(p)
        entries[f"doc:{rel}"], files[f"doc:{rel}"] = (st.st_mtime, st.st_size), p
    changed, gone = _stale(conn, "documents", entries)
    for item_id in changed:
        p = files[item_id]
        sha = documents.sha256_of(p)
        text = documents.text_for(p, sha) if extract else documents.cached_text(sha)
        rel = item_id.removeprefix("doc:")
        media = documents.media_of(p)
        conn.execute("BEGIN IMMEDIATE")
        _put(conn, {"id": item_id, "scope": "documents", "folder": rel.split("/")[0], "kind": media,
                    "title": p.name, "summary": rel, "mtime": entries[item_id][0], "size": entries[item_id][1],
                    "sha256": sha, "fm": json.dumps({"media": media, "mime": documents.mime_of(p)})},
             text or "", [("media", media)], [])
        conn.execute("COMMIT")
    if gone:
        conn.execute("BEGIN IMMEDIATE")
        for item_id in gone:
            _delete(conn, item_id)
        conn.execute("COMMIT")
    return len(changed) + len(gone)


def reindex(paths: list[str]) -> None:
    """Refresh specific vault pages right after a write, so agents see their own changes."""
    pages = [p.removesuffix(".md") for p in paths if p.split("/")[0] in PAGE_DIRS and p.endswith(".md")]
    if not pages:
        return
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        for path in pages:
            index_page(conn, path)
        conn.execute("COMMIT")


def known_hashes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT sha256 FROM items WHERE sha256 IS NOT NULL")}


# --- queries ---------------------------------------------------------------------------

def _fts_query(text: str, phrase: bool) -> str:
    if phrase:
        return '"' + text.replace('"', '""') + '"'
    words = re.findall(r"\w+", text)
    return " ".join(f'"{w}"*' for w in words)


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("invalid cursor; start again without one") from None


def search(conn: sqlite3.Connection, query: str = "", scope: str = "all", filters: dict | None = None,
           mode: str = "words", order_by: str | None = None, descending: bool = True,
           limit: int = 10, offset: int = 0) -> tuple[list[sqlite3.Row], bool]:
    """Ranked when there is a query (mode words|phrase), regex scan for mode=regex,
    otherwise a filtered listing. Returns (rows, has_more)."""
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {', '.join(SCOPES)}")
    where, args = [], []
    if SCOPES[scope]:
        where.append(SCOPES[scope])
    ranked = bool(query.strip()) and mode in ("words", "phrase")
    if ranked:
        expr = _fts_query(query, mode == "phrase")
        if not expr:
            raise ValueError("query needs at least one word")
        where.append("fts MATCH ?")
        args.append(expr)
    elif query.strip() and mode == "regex":
        where.append("(regexp(?, f.body) OR regexp(?, f.title))")
        args += [query, query]
    for key, want in (filters or {}).items():
        clause, values = _filter(key, want)
        where.append(clause)
        args += values
    select = ("SELECT i.*, bm25(fts, 0.0, 10.0, 4.0, 1.0) AS score, "
              "snippet(fts, -1, '«', '»', '…', 14) AS snippet FROM fts f JOIN items i ON i.id = f.id")
    if not ranked:
        select = "SELECT i.*, NULL AS score, substr(f.body, 1, 200) AS snippet FROM items i JOIN fts f ON f.id = i.id"
    sql = select + (" WHERE " + " AND ".join(where) if where else "")
    if ranked and not order_by:
        sql += " ORDER BY score"
    else:
        column = ORDER.get(order_by or "modified")
        if column is None:
            raise ValueError(f"order_by must be one of {', '.join(ORDER)}")
        sql += f" ORDER BY {column} IS NULL, {column} {'DESC' if descending else 'ASC'}"
    sql += " LIMIT ? OFFSET ?"
    rows = conn.execute(sql, [*args, limit + 1, offset]).fetchall()
    return rows[:limit], len(rows) > limit


def _filter(key: str, want) -> tuple[str, list]:
    if key in ("links_to", "backlinks_of"):
        return "i.id IN (SELECT src FROM links WHERE dst = ?)", [str(want).removesuffix(".md")]
    if key == "linked_from":
        return "i.id IN (SELECT dst FROM links WHERE src = ?)", [str(want).removesuffix(".md")]
    if key == "folder":  # notes, sources, projects, files, or a top-level documents folder
        return "i.folder = ?", [str(want).strip("/")]
    if key == "path_prefix":  # e.g. "University/Courses" or "notes/i-"
        prefix = str(want).strip("/")
        return "(i.id LIKE ? OR i.id LIKE ?)", [f"{prefix}%", f"doc:{prefix}%"]
    if key in LIST_FACETS:
        values = want if isinstance(want, list) else [want]
        clause = " AND ".join("i.id IN (SELECT id FROM facets WHERE field = ? AND value = ?)" for _ in values)
        return clause, [x for v in values for x in (key, str(v))]
    column = {"kind": "i.kind", "status": "i.status", "as_of": "i.as_of", "reviewed": "i.reviewed"}.get(
        key, f"json_extract(i.fm, '$.{re.sub(r'[^A-Za-z0-9_]', '', key)}')")
    if want is None:
        return f"({column} IS NULL OR {column} = '')", []
    if want == "*":
        return f"({column} IS NOT NULL AND {column} != '')", []
    if isinstance(want, dict):  # {"before": "2026-01-01"} / {"after": ...}
        ops = {"before": "<", "after": ">", "from": ">=", "until": "<="}
        parts = [(f"{column} {ops[k]} ?", v) for k, v in want.items() if k in ops]
        return " AND ".join(p for p, _ in parts) or "1", [v for _, v in parts]
    return f"{column} = ?", [str(want)]


def suggest(conn: sqlite3.Connection, wanted: str, limit: int = 3) -> list[str]:
    """Ids that share any word with `wanted` (for "did you mean" on a miss)."""
    words = [w for w in re.findall(r"\w+", wanted.split(":", 1)[-1]) if w not in PAGE_DIRS and len(w) > 1]
    if not words:
        return []
    expr = " OR ".join(f'"{w}"*' for w in words)
    rows = conn.execute("SELECT id FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 0.0, 10.0, 4.0, 1.0) LIMIT ?",
                        (expr, limit)).fetchall()
    return [r[0] for r in rows]


def backlinks(conn: sqlite3.Connection, item_id: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT src FROM links WHERE dst = ? ORDER BY src", (item_id,))]


def get(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT i.*, f.body FROM items i JOIN fts f ON f.id = i.id WHERE i.id = ?",
                        (item_id,)).fetchone()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build or refresh the vault index.")
    ap.add_argument("--rebuild", action="store_true", help="delete the index and build it from scratch")
    args = ap.parse_args()
    if args.rebuild:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{config.index_path()}{suffix}").unlink(missing_ok=True)
    with connect() as conn:
        n = sync_vault(conn) + sync_documents(conn, extract=True)
    print(f"indexed {n} changes into {config.index_path()}")


if __name__ == "__main__":
    main()
