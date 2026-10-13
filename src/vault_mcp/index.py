"""A derived SQLite index over the vault, its history and the documents tree.

Markdown and git stay the source of truth; this database can be deleted at any time and
rebuilt (`vault-index --rebuild`). Agents query it read-only through the `sql` tool, so its
tables are the vault's public data model:

    notes         every page: id (path), uid, folder, kind, status, title, summary, tags (JSON),
                  as_of, reviewed, blob, sha256, rev, mtime, size, fm (all frontmatter, JSON)
    documents     the documents tree: id (doc:<path>), path, folder, name, media, mime, size, sha256
    links         src -> dst, field (body, embed, or the frontmatter field: source, about, ...)
    tags          id, tag                      (view over facets: tags, categories, kind, ...)
    files         file notes with name, mime, size, blob and a signed download url (view)
    fts           full text (title, summary, body) of notes, file contents and documents
    commits       sha, date, author, message;  commit_files: sha, id
    problems      kind, id, detail             (lint findings, refreshed by the worker)

Incremental sync compares (mtime, size) with what is indexed, so a pass over an unchanged
vault only stats files. WAL lets the MCP server and the worker share it.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from . import config, documents, repo, signing
from .lint import Problem
from .vault import PAGE_DIRS, page_paths, read_page

SCHEMA_VERSION = "2"
LINK_FIELDS = ("source", "about", "related", "supersedes", "projects")

DDL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE notes(
  id TEXT PRIMARY KEY, uid TEXT, folder TEXT, kind TEXT, status TEXT, title TEXT, summary TEXT, tags TEXT,
  as_of TEXT, reviewed TEXT, blob TEXT, sha256 TEXT, rev TEXT, mtime REAL, size INTEGER, fm TEXT
);
CREATE TABLE documents(
  id TEXT PRIMARY KEY, path TEXT, folder TEXT, name TEXT, media TEXT, mime TEXT, size INTEGER, sha256 TEXT,
  mtime REAL
);
CREATE TABLE links(src TEXT NOT NULL, dst TEXT NOT NULL, field TEXT NOT NULL);
CREATE TABLE facets(id TEXT NOT NULL, field TEXT NOT NULL, value TEXT NOT NULL);
CREATE TABLE commits(sha TEXT PRIMARY KEY, date TEXT, author TEXT, message TEXT);
CREATE TABLE commit_files(sha TEXT NOT NULL, id TEXT NOT NULL);
CREATE TABLE problems(kind TEXT NOT NULL, id TEXT NOT NULL, detail TEXT);
CREATE VIRTUAL TABLE fts USING fts5(id UNINDEXED, title, summary, body, tokenize='unicode61 remove_diacritics 2');
CREATE VIEW tags AS SELECT id, value AS tag FROM facets WHERE field = 'tags';
CREATE VIEW files AS
  SELECT id, title, json_extract(fm, '$.original_name') AS name, json_extract(fm, '$.mime') AS mime,
         json_extract(fm, '$.size') AS size, tags, summary, blob, sha256,
         link(blob, json_extract(fm, '$.original_name')) AS url
  FROM notes WHERE folder = 'files';
CREATE INDEX notes_uid ON notes(uid);
CREATE INDEX notes_folder ON notes(folder);
CREATE INDEX facets_lookup ON facets(field, value);
CREATE INDEX facets_id ON facets(id);
CREATE INDEX links_dst ON links(dst);
CREATE INDEX links_src ON links(src);
CREATE INDEX commit_files_id ON commit_files(id);
"""


def _regexp(pattern: str, value: str | None) -> bool:
    return value is not None and re.search(pattern, value, re.I | re.M) is not None


def _link(target: str | None, name: str | None = None) -> str | None:
    """SQL link(blob_or_doc_id [, name]): a signed download URL, valid for an hour."""
    if not target:
        return None
    try:
        if target.startswith("doc:"):
            return signing.document_link(target.removeprefix("doc:"))
        return signing.blob_link(target, name or target.rsplit("/", 1)[-1])
    except documents.DocumentError:
        return None


def _functions(conn: sqlite3.Connection) -> None:
    conn.create_function("regexp", 2, _regexp, deterministic=True)
    conn.create_function("link", 1, _link)
    conn.create_function("link", 2, _link)


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = config.index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _functions(conn)
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
    for kind in ("view", "table"):
        for (name,) in conn.execute(f"SELECT name FROM sqlite_master WHERE type = '{kind}' "
                                    "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'fts_%'").fetchall():
            conn.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
    for statement in filter(str.strip, DDL.split(";")):
        conn.execute(statement)
    conn.execute("INSERT INTO meta VALUES ('schema', ?)", (SCHEMA_VERSION,))
    conn.execute("COMMIT")


# --- keeping it current ---------------------------------------------------------------

def _delete(conn: sqlite3.Connection, item_id: str) -> None:
    for table, col in (("notes", "id"), ("documents", "id"), ("facets", "id"), ("links", "src"), ("fts", "id")):
        conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (item_id,))


def _insert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    conn.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))


def _str(value) -> str | None:
    return None if value in (None, "", []) else str(value)


def index_page(conn: sqlite3.Connection, path: str) -> None:
    """(Re)index one vault page, or drop it if the file is gone."""
    f = config.VAULT_ROOT / f"{path}.md"
    _delete(conn, path)
    if not f.exists():
        return
    data, st = f.read_bytes(), f.stat()
    page = read_page(config.VAULT_ROOT, path)
    m, body = page.meta, page.body
    if path.startswith("files/") and m.get("blob") and m.get("sha256"):
        blob = config.VAULT_ROOT / str(m["blob"])
        if blob.exists():
            body += "\n\n" + (documents.text_for(blob, str(m["sha256"])) or "")
    tags = [str(t) for t in m.get("tags") or []] if isinstance(m.get("tags"), list) else []
    _insert(conn, "notes", {
        "id": path, "uid": page.sid, "folder": path.split("/")[0], "kind": _str(m.get("kind")),
        "status": _str(m.get("status")), "title": page.title, "summary": _str(m.get("summary")),
        "tags": json.dumps(tags), "as_of": _str(m.get("as_of")), "reviewed": _str(m.get("reviewed")),
        "blob": _str(m.get("blob")), "sha256": _str(m.get("sha256")), "rev": repo.rev_of(data),
        "mtime": st.st_mtime, "size": st.st_size, "fm": json.dumps(m, default=str),
    })
    conn.execute("INSERT INTO fts (id, title, summary, body) VALUES (?, ?, ?, ?)",
                 (path, page.title, " ".join(filter(None, [_str(m.get("summary")), *tags])), body))
    facets = [("tags", t) for t in tags]
    facets += [("categories", str(c)) for c in m.get("categories") or [] if isinstance(m.get("categories"), list)]
    facets += [(k, str(m[k])) for k in ("kind", "status", "origin", "media", "source_kind") if m.get(k)]
    conn.executemany("INSERT INTO facets VALUES (?, ?, ?)", [(path, k, v) for k, v in facets])
    links = [(field, t) for field in LINK_FIELDS for t in page.field_links(field)]
    links += [("body", t) for t in sorted(page.links - {t for _, t in links})]
    links += [("embed", t) for t in sorted(page.embeds)]
    conn.executemany("INSERT INTO links VALUES (?, ?, ?)", [(path, t, field) for field, t in links])


def _stale(conn, table: str, entries: dict[str, tuple[float, int]]) -> tuple[list[str], list[str]]:
    known = {r["id"]: (r["mtime"], r["size"]) for r in conn.execute(f"SELECT id, mtime, size FROM {table}")}
    changed = [i for i, stat in entries.items() if known.get(i) != stat]
    return changed, [i for i in known if i not in entries]


def sync_vault(conn: sqlite3.Connection) -> int:
    entries = {}
    for path in page_paths(config.VAULT_ROOT):
        st = (config.VAULT_ROOT / f"{path}.md").stat()
        entries[path] = (st.st_mtime, st.st_size)
    changed, gone = _stale(conn, "notes", entries)
    if changed or gone:
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
        st, item_id = p.stat(), f"doc:{documents.relative(p)}"
        entries[item_id], files[item_id] = (st.st_mtime, st.st_size), p
    changed, gone = _stale(conn, "documents", entries)
    for item_id in changed:
        p, rel = files[item_id], item_id.removeprefix("doc:")
        sha = documents.sha256_of(p)
        text = documents.text_for(p, sha) if extract else documents.cached_text(sha)
        conn.execute("BEGIN IMMEDIATE")
        _delete(conn, item_id)
        _insert(conn, "documents", {
            "id": item_id, "path": rel, "folder": rel.split("/")[0], "name": p.name, "media": documents.media_of(p),
            "mime": documents.mime_of(p), "size": entries[item_id][1], "sha256": sha, "mtime": entries[item_id][0]})
        conn.execute("INSERT INTO fts (id, title, summary, body) VALUES (?, ?, ?, ?)",
                     (item_id, p.name, rel, text or ""))
        conn.execute("COMMIT")
    if gone:
        conn.execute("BEGIN IMMEDIATE")
        for item_id in gone:
            _delete(conn, item_id)
        conn.execute("COMMIT")
    return len(changed) + len(gone)


def sync_commits(conn: sqlite3.Connection) -> int:
    """Add commits made since the last sync (all of them on a fresh index)."""
    last = conn.execute("SELECT value FROM meta WHERE key = 'last_commit'").fetchone()
    head = repo.git("rev-parse", "HEAD").strip()
    if last and last[0] == head:
        return 0
    span = f"{last[0]}..HEAD" if last else "HEAD"
    try:
        out = repo.git("log", span, "--name-only", "--format=\x1e%h\x1f%cI\x1f%an\x1f%s")
    except repo.GitError:  # history rewritten: start over
        conn.execute("DELETE FROM commits")
        conn.execute("DELETE FROM commit_files")
        out = repo.git("log", "HEAD", "--name-only", "--format=\x1e%h\x1f%cI\x1f%an\x1f%s")
    records = [r for r in out.split("\x1e") if r.strip()]
    conn.execute("BEGIN IMMEDIATE")
    for record in records:
        header, *paths = record.strip("\n").split("\n")
        sha, date, author, message = header.split("\x1f", 3)
        conn.execute("INSERT OR REPLACE INTO commits VALUES (?, ?, ?, ?)", (sha, date, author, message))
        conn.executemany("INSERT INTO commit_files VALUES (?, ?)",
                         [(sha, p.removesuffix(".md")) for p in paths if p.strip()])
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('last_commit', ?)", (head,))
    conn.execute("COMMIT")
    return len(records)


def set_problems(conn: sqlite3.Connection, problems: list[Problem]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM problems")
    conn.executemany("INSERT INTO problems VALUES (?, ?, ?)", problems)
    conn.execute("COMMIT")


def reindex(paths: list[str]) -> None:
    """Refresh specific vault pages right after a write, so agents see their own changes."""
    pages = [p.removesuffix(".md") for p in paths if p.split("/")[0] in PAGE_DIRS and p.endswith(".md")]
    with connect() as conn:
        if pages:
            conn.execute("BEGIN IMMEDIATE")
            for path in pages:
                index_page(conn, path)
            conn.execute("COMMIT")
        sync_commits(conn)


def known_hashes(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT sha256 FROM notes WHERE sha256 IS NOT NULL "
                                       "UNION SELECT sha256 FROM documents")}


# --- reading ---------------------------------------------------------------------------

def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"])
    except (ValueError, KeyError, TypeError):
        raise ValueError("invalid cursor; start again without one") from None


def fts_query(text: str, any_term: bool = False) -> str:
    """Words match as prefixes, "quoted phrases" exactly; all terms must occur unless any_term."""
    phrases = [p.replace('"', "") for p in re.findall(r'"([^"]+)"', text)]
    words = re.findall(r"\w+", re.sub(r'"[^"]*"', " ", text))
    return (" OR " if any_term else " ").join([f'"{p}"' for p in phrases] + [f'"{w}"*' for w in words])


def search(conn: sqlite3.Connection, query: str, limit: int, offset: int) -> tuple[list[sqlite3.Row], bool]:
    """Pages with all the terms; when none has all of them, pages with any, best (BM25) first."""
    expr = fts_query(query)
    if not expr:
        raise ValueError("search needs at least one word")
    if not conn.execute("SELECT 1 FROM fts WHERE fts MATCH ? LIMIT 1", (expr,)).fetchone():
        expr = fts_query(query, any_term=True)
    rows = conn.execute(
        "SELECT id, title, snippet(fts, 3, '', '', '…', 24) AS snippet FROM fts WHERE fts MATCH ? "
        "ORDER BY bm25(fts, 0.0, 10.0, 4.0, 1.0) LIMIT ? OFFSET ?", (expr, limit + 1, offset)).fetchall()
    return rows[:limit], len(rows) > limit


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


def by_uid(conn: sqlite3.Connection, uid: str) -> str | None:
    row = conn.execute("SELECT id FROM notes WHERE uid = ?", (uid,)).fetchone()
    return row[0] if row else None


# --- agents' SQL: read-only, one statement, bounded ------------------------------------

_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
            getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "data_version"}  # FTS5 reads data_version
CELL_MAX = 2000
RESULT_MAX = 100_000  # characters across all returned cells


def _authorize(action: int, arg1, arg2, db, source) -> int:
    if action in _ALLOWED or (action == sqlite3.SQLITE_PRAGMA and arg1 in _PRAGMAS):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _cell(value):
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    if isinstance(value, str) and len(value) > CELL_MAX:
        return value[:CELL_MAX] + f"… [{len(value) - CELL_MAX} more characters; select substr(...) to read on]"
    return value


def run_sql(sql: str, params: list | dict | None, limit: int, offset: int) -> tuple[list[str], list[dict], bool]:
    """(columns, rows as dicts, more rows exist). Raises ValueError with a message meant for the agent."""
    with connect():  # make sure the index exists and has the current schema
        pass
    conn = sqlite3.connect(f"file:{config.index_path()}?mode=ro", uri=True, timeout=5, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = 1")
        _functions(conn)
        conn.set_authorizer(_authorize)
        deadline = time.monotonic() + config.SQL_TIMEOUT_SECONDS
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        try:
            cur = conn.execute(sql, params or [])
            if cur.description is None:
                raise ValueError("only queries (SELECT, WITH) are allowed; change the vault with `write`")
            columns = [d[0] for d in cur.description]
            for _ in range(offset):
                if cur.fetchone() is None:
                    break
            rows, size = [], 0
            for raw in cur.fetchmany(limit + 1):
                row = {c: _cell(v) for c, v in zip(columns, raw, strict=True)}
                size += sum(len(str(v)) for v in row.values())
                if len(rows) == limit or (rows and size > RESULT_MAX):
                    return columns, rows, True
                rows.append(row)
            return columns, rows, False
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e):
                raise ValueError(f"query ran longer than {config.SQL_TIMEOUT_SECONDS:g}s; narrow it") from None
            if "not authorized" in str(e):
                raise ValueError("not allowed: the index is read-only (SELECT/WITH only, no ATTACH or PRAGMA writes)") \
                    from None
            raise ValueError(f"SQL error: {e}") from None
        except (sqlite3.ProgrammingError, sqlite3.DatabaseError) as e:
            raise ValueError(f"SQL error: {e}") from None
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Build or refresh the vault index.")
    ap.add_argument("--rebuild", action="store_true", help="delete the index and build it from scratch")
    args = ap.parse_args()
    if args.rebuild:
        for suffix in ("", "-wal", "-shm"):
            Path(f"{config.index_path()}{suffix}").unlink(missing_ok=True)
    with connect() as conn:
        n = sync_vault(conn) + sync_documents(conn, extract=True) + sync_commits(conn)
    print(f"indexed {n} changes into {config.index_path()}")


if __name__ == "__main__":
    main()
