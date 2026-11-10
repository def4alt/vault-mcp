"""The vault is one SQLite file. Six tables hold canonical data; the rest is derived.

    pages       id, title, body, private, updated         every page: notes/x, sources/x, projects/x, files/x
    facts       id, subject, claim, valid_from, valid_until, confirmed, source, note
    questions   id, question, about, asked, asked_by, answer, answered     what agents need me to answer
    files       id (its page), blob, sha256, mime, size, name, versions    stored files, described by their page
    queries     name, sql, about                           saved queries: tools as data
    log         time, actor, message, changed, sha         every write, with the git commit of its export

Derived, rebuilt from the above (`vault-import --export`; the worker indexes documents): links, chunks, fts,
documents, blobs, and the views current_facts and problems.

Agents read with one SELECT (`run_sql`) and change rows with ordinary INSERT/UPDATE/DELETE statements
(`apply`): one call is one transaction, one log row and one git commit of the Markdown export.
Triggers put every page a change touches into `touched`; `drain` re-derives and re-exports those.
"""
from __future__ import annotations

import base64
import contextlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from . import config, documents, embed, markdown, pages, repo, signing

log = logging.getLogger("vault-store")
SCHEMA_VERSION = "2"
DATE = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
WRITABLE = {"pages", "facts", "questions", "queries", "blobs"}
MANY = 20  # rows one statement may change unless it says `many`
STALE_DAYS = 365

DDL = f"""
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE pages(
  id TEXT PRIMARY KEY CONSTRAINT "page id: letters, digits, - and _ in folders, e.g. notes/acme"
    CHECK (id GLOB '[A-Za-z0-9]*' AND id NOT GLOB '*[^A-Za-z0-9/_-]*'),
  title TEXT NOT NULL CONSTRAINT "a page needs a title" CHECK (length(trim(title)) > 0),
  body TEXT NOT NULL DEFAULT ''
    CONSTRAINT "facts are rows of the facts table: do not paste the exported table into a body"
    CHECK (instr(body, '| # | Claim | From | Until | Confirmed | Source | Note |') = 0),
  private INTEGER NOT NULL DEFAULT 0 CONSTRAINT "private is 0 or 1" CHECK (private IN (0, 1)),
  updated TEXT NOT NULL DEFAULT ({NOW})
);
CREATE TABLE facts(
  id INTEGER PRIMARY KEY,
  subject TEXT NOT NULL REFERENCES pages(id) ON UPDATE CASCADE ON DELETE CASCADE,
  claim TEXT NOT NULL CONSTRAINT "a fact needs a claim" CHECK (length(trim(claim)) > 0),
  valid_from TEXT NOT NULL CONSTRAINT "valid_from must be YYYY-MM-DD" CHECK (valid_from {DATE}),
  valid_until TEXT CONSTRAINT "valid_until must be NULL or YYYY-MM-DD, not before valid_from"
    CHECK (valid_until IS NULL OR (valid_until {DATE} AND valid_until >= valid_from)),
  confirmed TEXT CONSTRAINT "confirmed must be NULL or the YYYY-MM-DD date I confirmed it"
    CHECK (confirmed IS NULL OR confirmed {DATE}),
  source TEXT,
  note TEXT
);
CREATE TABLE questions(
  id INTEGER PRIMARY KEY,
  question TEXT NOT NULL CONSTRAINT "a question needs text" CHECK (length(trim(question)) > 0),
  about TEXT,
  asked TEXT NOT NULL DEFAULT (date('now')),
  asked_by TEXT,
  answer TEXT,
  answered TEXT
);
CREATE TABLE files(
  id TEXT PRIMARY KEY REFERENCES pages(id) ON UPDATE CASCADE ON DELETE CASCADE,
  blob TEXT NOT NULL, sha256 TEXT NOT NULL, mime TEXT, size INTEGER, name TEXT,
  versions TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE queries(name TEXT PRIMARY KEY, sql TEXT NOT NULL, about TEXT);
CREATE TABLE log(
  id INTEGER PRIMARY KEY, time TEXT NOT NULL DEFAULT ({NOW}), actor TEXT, message TEXT, changed TEXT, sha TEXT,
  key TEXT
);
CREATE TABLE touched(id TEXT PRIMARY KEY) WITHOUT ROWID;

-- derived
CREATE TABLE links(src TEXT NOT NULL, dst TEXT NOT NULL);
CREATE TABLE chunks(id INTEGER PRIMARY KEY, ref TEXT NOT NULL, page TEXT NOT NULL, title TEXT, text TEXT NOT NULL,
                    embedding BLOB);
CREATE VIRTUAL TABLE fts USING fts5(title, text, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE documents(id TEXT PRIMARY KEY, path TEXT, folder TEXT, name TEXT, media TEXT, mime TEXT, size INTEGER,
                       sha256 TEXT, mtime REAL);
CREATE TABLE blobs(name TEXT PRIMARY KEY, size INTEGER);
CREATE INDEX facts_subject ON facts(subject);
CREATE INDEX links_dst ON links(dst);
CREATE INDEX links_src ON links(src);
CREATE INDEX chunks_page ON chunks(page);
CREATE INDEX files_sha ON files(sha256);

CREATE VIEW current_facts AS
  SELECT * FROM facts WHERE valid_from <= date('now') AND (valid_until IS NULL OR valid_until >= date('now'));
CREATE VIEW problems AS
  SELECT 'broken-link' AS kind, src AS id, 'links to ' || dst || ', which does not exist' AS detail FROM links
   WHERE dst NOT LIKE 'doc:%' AND dst NOT LIKE 'blobs/%' AND NOT EXISTS (SELECT 1 FROM pages WHERE id = dst)
  UNION ALL
  SELECT 'broken-link', src, 'embeds ' || dst || ', which is not stored' FROM links
   WHERE dst LIKE 'blobs/%' AND NOT EXISTS (SELECT 1 FROM blobs WHERE 'blobs/' || name = dst)
  UNION ALL
  SELECT 'stale', subject || '#fact-' || id, claim || ' (confirmed ' || confirmed || ', over a year ago)' FROM facts
   WHERE valid_until IS NULL AND confirmed IS NOT NULL AND confirmed < date('now', '-{STALE_DAYS} days')
  UNION ALL
  SELECT 'question', 'questions#' || id, question FROM questions WHERE answer IS NULL
  UNION ALL
  SELECT 'orphan', id, 'no page links here and it links to none' FROM pages p
   WHERE id LIKE 'notes/%' AND NOT EXISTS (SELECT 1 FROM links WHERE src = p.id OR dst = p.id)
  UNION ALL
  SELECT 'undistilled', id, 'no page or fact cites it yet' FROM pages p
   WHERE (id LIKE 'sources/%' OR id LIKE 'files/%') AND NOT EXISTS (SELECT 1 FROM links WHERE dst = p.id)
  UNION ALL
  SELECT 'blob', 'blobs/' || name, 'no file page describes it (describe it, or DELETE FROM blobs WHERE name = ...)'
    FROM blobs b WHERE NOT EXISTS (SELECT 1 FROM files WHERE blob = 'blobs/' || b.name
                                   OR instr(versions, '"blobs/' || b.name || '"') > 0)
  UNION ALL
  SELECT 'blob', id, 'its file ' || blob || ' is missing' FROM files f
   WHERE NOT EXISTS (SELECT 1 FROM blobs WHERE 'blobs/' || name = f.blob);

-- every change marks the pages it touches; drain() re-derives and re-exports them
CREATE TRIGGER pages_ins AFTER INSERT ON pages BEGIN INSERT OR IGNORE INTO touched VALUES (NEW.id); END;
CREATE TRIGGER pages_del AFTER DELETE ON pages BEGIN INSERT OR IGNORE INTO touched VALUES (OLD.id); END;
CREATE TRIGGER pages_upd AFTER UPDATE ON pages BEGIN
  INSERT OR IGNORE INTO touched VALUES (NEW.id);
  INSERT OR IGNORE INTO touched VALUES (OLD.id);
  UPDATE pages SET updated = {NOW} WHERE id = NEW.id AND NEW.updated = OLD.updated;
END;
CREATE TRIGGER pages_rename AFTER UPDATE OF id ON pages WHEN NEW.id <> OLD.id BEGIN
  UPDATE pages SET body = replace(replace(body, '[[' || OLD.id || ']]', '[[' || NEW.id || ']]'),
                                  '[[' || OLD.id || '|', '[[' || NEW.id || '|')
   WHERE instr(body, '[[' || OLD.id) > 0;
  UPDATE facts SET source = replace(source, '[[' || OLD.id || ']]', '[[' || NEW.id || ']]')
   WHERE instr(source, '[[' || OLD.id || ']]') > 0;
END;
CREATE TRIGGER facts_ins AFTER INSERT ON facts BEGIN INSERT OR IGNORE INTO touched VALUES (NEW.subject); END;
CREATE TRIGGER facts_del AFTER DELETE ON facts BEGIN INSERT OR IGNORE INTO touched VALUES (OLD.subject); END;
CREATE TRIGGER facts_upd AFTER UPDATE ON facts BEGIN
  INSERT OR IGNORE INTO touched VALUES (NEW.subject);
  INSERT OR IGNORE INTO touched VALUES (OLD.subject);
END;
CREATE TRIGGER files_ins AFTER INSERT ON files BEGIN INSERT OR IGNORE INTO touched VALUES (NEW.id); END;
CREATE TRIGGER files_upd AFTER UPDATE ON files BEGIN INSERT OR IGNORE INTO touched VALUES (NEW.id); END;
CREATE TRIGGER questions_ins AFTER INSERT ON questions BEGIN INSERT OR IGNORE INTO touched VALUES ('questions'); END;
CREATE TRIGGER questions_upd AFTER UPDATE ON questions BEGIN INSERT OR IGNORE INTO touched VALUES ('questions'); END;
CREATE TRIGGER questions_del AFTER DELETE ON questions BEGIN INSERT OR IGNORE INTO touched VALUES ('questions'); END;
CREATE TRIGGER queries_ins AFTER INSERT ON queries BEGIN INSERT OR IGNORE INTO touched VALUES ('queries'); END;
CREATE TRIGGER queries_upd AFTER UPDATE ON queries BEGIN INSERT OR IGNORE INTO touched VALUES ('queries'); END;
CREATE TRIGGER queries_del AFTER DELETE ON queries BEGIN INSERT OR IGNORE INTO touched VALUES ('queries'); END;
-- DELETE FROM blobs removes the file itself (drain unlinks it), unless a file page still uses it
CREATE TRIGGER blobs_keep BEFORE DELETE ON blobs
  WHEN NOT EXISTS (SELECT 1 FROM meta WHERE key = 'syncing-blobs')
   AND EXISTS (SELECT 1 FROM files WHERE blob = 'blobs/' || OLD.name
                                      OR instr(versions, '"blobs/' || OLD.name || '"') > 0)
  BEGIN
    SELECT RAISE(ABORT, 'this blob belongs to a file page (its file or an earlier version); delete that page first');
  END;
CREATE TRIGGER blobs_del AFTER DELETE ON blobs WHEN NOT EXISTS (SELECT 1 FROM meta WHERE key = 'syncing-blobs')
  BEGIN INSERT OR IGNORE INTO touched VALUES ('blob:' || OLD.name); END;
"""


# --- connection ------------------------------------------------------------------------

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
    conn.create_function("page_url", 1, pages.url)
    conn.create_function("embed", 1, embed.query, deterministic=True)
    conn.create_function("cosine", 2, embed.cosine, deterministic=True)


@contextlib.contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _functions(conn)
        try:
            conn.execute("SELECT 1 FROM meta LIMIT 1")
        except sqlite3.OperationalError:
            _create(conn)
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()[0]
        if version != SCHEMA_VERSION:
            raise RuntimeError(f"unsupported database schema {version}; expected {SCHEMA_VERSION}")
        yield conn
    finally:
        conn.close()


def _create(conn: sqlite3.Connection) -> None:
    """Create the schema once; safe when the server and the worker start together."""
    try:
        conn.executescript(f"BEGIN IMMEDIATE;{DDL}INSERT INTO meta VALUES ('schema', '{SCHEMA_VERSION}');COMMIT;")
    except sqlite3.OperationalError as e:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        if "already exists" not in str(e):
            raise


# --- derived data ------------------------------------------------------------------------

PIECE = 900  # characters a chunk aims for


def split(text: str, limit: int = 80) -> list[str]:
    """Paragraphs merged into chunks of about PIECE characters."""
    out, current = [], ""
    for para in filter(None, (p.strip() for p in re.split(r"\n\s*\n", text))):
        if current and len(current) + len(para) > PIECE:
            out.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
        while len(current) > 2 * PIECE:
            out.append(current[:2 * PIECE])
            current = current[2 * PIECE:]
    if current:
        out.append(current)
    return out[:limit]


def fact_text(f: sqlite3.Row) -> str:
    state = f"confirmed {f['confirmed']}" if f["confirmed"] else "unreviewed"
    period = f"from {f['valid_from']} until {f['valid_until']}" if f["valid_until"] else \
        f"from {f['valid_from']}, open-ended"
    return " ".join(filter(None, [f["claim"], f["note"], f"({state}, {period})"]))


def _set_chunks(conn: sqlite3.Connection, page: str, title: str, pieces: list[tuple[str, str]]) -> None:
    known = dict(conn.execute("SELECT text, embedding FROM chunks WHERE page = ? AND title IS ? "
                              "AND embedding IS NOT NULL", (page, title)))
    conn.execute("DELETE FROM fts WHERE rowid IN (SELECT id FROM chunks WHERE page = ?)", (page,))
    conn.execute("DELETE FROM chunks WHERE page = ?", (page,))
    for ref, text in pieces:
        # unchanged text keeps its vector; new text is embedded here by the hashed stand-in, later by the worker
        vector = known.get(text) or (None if config.EMBED_MODEL else embed.passage(title, text))
        cur = conn.execute("INSERT INTO chunks (ref, page, title, text, embedding) VALUES (?, ?, ?, ?, ?)",
                           (ref, page, title, text, vector))
        conn.execute("INSERT INTO fts (rowid, title, text) VALUES (?, ?, ?)", (cur.lastrowid, title, text))


def derive(conn: sqlite3.Connection, pid: str) -> None:
    """Rebuild links, chunks and embeddings of one page (or drop them if the page is gone)."""
    conn.execute("DELETE FROM links WHERE src = ?", (pid,))
    page = conn.execute("SELECT * FROM pages WHERE id = ?", (pid,)).fetchone()
    if page is None:
        _set_chunks(conn, pid, "", [])
        return
    facts = conn.execute("SELECT * FROM facts WHERE subject = ? ORDER BY id", (pid,)).fetchall()
    text = "\n".join([page["body"], *(f"{f['source'] or ''} {f['note'] or ''}" for f in facts)])
    targets = {t for t in markdown.link_targets(text) if t != pid}
    conn.executemany("INSERT INTO links VALUES (?, ?)", [(pid, t) for t in sorted(targets)])
    if "/" not in pid:  # AGENTS and index are instructions and navigation, not knowledge to search
        _set_chunks(conn, pid, "", [])
        return
    pieces = [(pid, t) for t in split(page["body"])] or [(pid, page["title"])]
    pieces += [(f"{pid}#fact-{f['id']}", fact_text(f)) for f in facts]
    _set_chunks(conn, pid, page["title"], pieces)


def drain(conn: sqlite3.Connection) -> list[str]:
    """Re-derive and re-export every touched page; returns the export paths that changed (for the commit)."""
    ids = [r[0] for r in conn.execute("SELECT id FROM touched ORDER BY id")]
    if not ids:
        return []
    paths: list[str] = []
    conn.execute("BEGIN IMMEDIATE")
    for item in ids:
        if item.startswith("blob:"):
            (config.blobs_root() / item.removeprefix("blob:")).unlink(missing_ok=True)
        elif item in ("questions", "queries"):
            paths.append(markdown.export_table(conn, item))
        else:
            derive(conn, item)
            paths.append(markdown.export_page(conn, item))
    conn.execute("DELETE FROM touched")
    conn.execute("COMMIT")
    return paths


def derive_all(conn: sqlite3.Connection) -> None:
    """Mark every page and saved table for re-derivation and export."""
    conn.execute("INSERT OR IGNORE INTO touched SELECT id FROM pages")
    conn.execute("INSERT OR IGNORE INTO touched VALUES ('questions'), ('queries')")


def sync_blobs(conn: sqlite3.Connection) -> None:
    """Mirror the blob directory into the blobs table."""
    root = config.blobs_root()
    on_disk = {f.name: f.stat().st_size for f in root.iterdir() if f.is_file() and not f.name.startswith(".")} \
        if root.is_dir() else {}
    known = {r["name"]: r["size"] for r in conn.execute("SELECT name, size FROM blobs")}
    if on_disk == known:
        return
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('syncing-blobs', '1')")  # the delete triggers stand down
    conn.execute("DELETE FROM blobs")
    conn.executemany("INSERT INTO blobs VALUES (?, ?)", list(on_disk.items()))
    conn.execute("DELETE FROM meta WHERE key = 'syncing-blobs'")
    conn.execute("COMMIT")


def sync_documents(conn: sqlite3.Connection) -> int:
    """Index the documents tree (read-only material): rows, chunks and embeddings."""
    drop = config.DOCS_ROOT / config.DROP_REL
    entries: dict[str, tuple[float, int]] = {}
    files: dict[str, Path] = {}
    for p in documents.walk(config.DOCS_ROOT):
        if drop in p.parents or p.suffix.lower() in documents.HTML:
            continue
        st, item_id = p.stat(), f"doc:{documents.relative(p)}"
        entries[item_id], files[item_id] = (st.st_mtime, st.st_size), p
    known = {r["id"]: (r["mtime"], r["size"]) for r in conn.execute("SELECT id, mtime, size FROM documents")}
    changed = [i for i, stat in entries.items() if known.get(i) != stat]
    gone = [i for i in known if i not in entries]
    for item_id in changed:
        p, rel = files[item_id], item_id.removeprefix("doc:")
        sha = documents.sha256_of(p)
        text = documents.text_for(p, sha)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            item_id, rel, rel.split("/")[0], p.name, documents.media_of(p), documents.mime_of(p),
            entries[item_id][1], sha, entries[item_id][0]))
        _set_chunks(conn, item_id, p.name, [(item_id, t) for t in split(text or "", 60)] or [(item_id, rel)])
        conn.execute("COMMIT")
    if gone:
        conn.execute("BEGIN IMMEDIATE")
        for item_id in gone:
            conn.execute("DELETE FROM documents WHERE id = ?", (item_id,))
            _set_chunks(conn, item_id, "", [])
        conn.execute("COMMIT")
    return len(changed) + len(gone)


def check_model(conn: sqlite3.Connection) -> None:
    """Vectors from another model cannot be compared: forget them all when the model changes."""
    row = conn.execute("SELECT value FROM meta WHERE key = 'embed_model'").fetchone()
    if row and row[0] == embed.model_id():
        return
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT OR REPLACE INTO meta VALUES ('embed_model', ?)", (embed.model_id(),))
    conn.execute("UPDATE chunks SET embedding = NULL")
    conn.execute("COMMIT")


def embed_backlog(conn: sqlite3.Connection, seconds: float = 20.0) -> int:
    """Give vectors to chunks that have none yet (new or changed text), for about `seconds`; the worker's job.
    Search works meanwhile: such chunks are found by keyword only."""
    deadline, done = time.monotonic() + seconds, 0
    while time.monotonic() < deadline:
        rows = conn.execute("SELECT id, page, title, text FROM chunks WHERE embedding IS NULL "
                            "ORDER BY (page LIKE 'doc:%'), id LIMIT 8").fetchall()
        if not rows:
            break
        vectors = [(embed.passage(r["title"], r["text"]), r["id"], r["page"], r["title"], r["text"])
                   for r in rows]  # computed outside any lock; a concurrent write may replace these chunks
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany("UPDATE chunks SET embedding = ? WHERE id = ? AND page = ? AND title IS ? "
                         "AND text = ? AND embedding IS NULL", vectors)
        conn.execute("COMMIT")
        done += len(rows)
    return done


# --- writing ---------------------------------------------------------------------------

def _write_auth():
    """One authorizer per statement: file metadata changes only through a page's FK cascade."""
    target = None

    def authorize(action: int, arg1, arg2, db, source) -> int:
        nonlocal target
        if action in _READ:
            return sqlite3.SQLITE_OK
        if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
            if source is not None:  # a trigger doing the bookkeeping
                return sqlite3.SQLITE_OK
            if target is None:
                target = arg1
            if arg1 in WRITABLE and (arg1 != "blobs" or action == sqlite3.SQLITE_DELETE):
                return sqlite3.SQLITE_OK
            if arg1 == "files" and target == "pages" and action != sqlite3.SQLITE_INSERT:
                return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return authorize


def _friendly(e: Exception) -> str:
    text = str(e)
    if "FOREIGN KEY" in text:
        return "FOREIGN KEY constraint failed: facts.subject must be the id of an existing page"
    if "not authorized" in text:
        return ("not allowed: write takes INSERT, UPDATE and DELETE on pages, facts, questions and queries "
                "(files come in through upload; DELETE FROM blobs removes an unused file)")
    return text


def apply(statements: list[tuple[str, list | dict | None, bool]], actor: str, message: str,
          key: str | None = None, author: str = "vault") -> dict:
    """Run (sql, params, many) statements as one transaction, then export and commit what they touched.
    Returns {"commit", "changed", "rows", "replayed"}. Raises ValueError with a message meant for the agent."""
    with repo.locked(), connect() as conn:
        if key and (done := conn.execute("SELECT sha, changed FROM log WHERE key = ?", (key,)).fetchone()):
            return {"commit": done["sha"], "changed": json.loads(done["changed"]), "rows": 0, "replayed": True}
        rows = 0
        conn.execute("BEGIN IMMEDIATE")
        try:
            for sql, params, many in statements:
                conn.set_authorizer(_write_auth())
                cur = conn.execute(sql, params or [])
                if cur.description is not None:
                    cur.close()
                    raise ValueError("write takes INSERT, UPDATE and DELETE without RETURNING; use sql to read")
                changed_rows = conn.execute("SELECT changes()").fetchone()[0]
                if changed_rows > MANY and not many:
                    raise ValueError(f"this statement would change {changed_rows} rows; narrow its WHERE, or pass "
                                      f"many=true if that is what you mean: {sql[:80]}")
                rows += changed_rows
        except (sqlite3.Error, ValueError) as e:
            conn.set_authorizer(None)
            conn.execute("ROLLBACK")
            raise ValueError(_friendly(e)) from None
        conn.set_authorizer(None)
        changed = [r[0] for r in conn.execute("SELECT id FROM touched ORDER BY id")]
        cur = conn.execute("INSERT INTO log (actor, message, changed, key) VALUES (?, ?, ?, ?)",
                           (actor, message, json.dumps(changed), key))
        conn.execute("COMMIT")
        sha = publish(conn, f"{message} ({actor})", author)
        conn.execute("UPDATE log SET sha = ? WHERE id = ?", (sha, cur.lastrowid))
        return {"commit": sha, "changed": changed, "rows": rows, "replayed": False}


def publish(conn: sqlite3.Connection, message: str, author: str = "vault") -> str:
    """Drain what is touched into the export and commit it. Call inside repo.locked()."""
    paths = drain(conn)
    return repo.commit(paths, message, author) if paths else "no changes"


# --- reading ---------------------------------------------------------------------------

def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        offset = json.loads(base64.urlsafe_b64decode(cursor.encode()))["o"]
        if type(offset) is not int or offset < 0:
            raise ValueError
        return offset
    except (ValueError, KeyError, TypeError):
        raise ValueError("invalid cursor; start again without one") from None


STOPWORDS = set("a an and are as at be by did do does for from had has have how i in is it me my of on or our that the "
                "their this to was we were what when where which who whom whose why will with you your".split())


def fts_query(text: str) -> str:
    """Any of the words (as prefixes) or "quoted phrases"; BM25 ranks rows with more of them first.
    Question words and other fillers are left out when anything else remains: meaning ranks those."""
    phrases = [p.replace('"', "") for p in re.findall(r'"([^"]+)"', text)]
    words = re.findall(r"\w+", re.sub(r'"[^"]*"', " ", text))
    content = [w for w in words if w.lower() not in STOPWORDS]
    return " OR ".join([f'"{p}"' for p in phrases] + [f'"{w}"*' for w in (content or words)])


def search(conn: sqlite3.Connection, query: str, limit: int, offset: int = 0) -> tuple[list[dict], bool]:
    """Hybrid search over chunks: keyword rank (BM25) and meaning rank (cosine) fused by reciprocal rank.
    Returns the best chunk per ref (a page, one of its facts, or a document)."""
    expr = fts_query(query)
    if not expr:
        raise ValueError("search needs at least one word")
    pool = max(50, 4 * (limit + offset))
    keyword = [r[0] for r in conn.execute(
        "SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY bm25(fts, 4.0, 1.0) LIMIT ?", (expr, pool))]
    try:
        vector = embed.query(query)
    except Exception:  # no model (first start without network): keywords alone still answer
        log.exception("embedding the query failed; searching by keyword only")
        vector = None
    meaning = embed.nearest(vector, conn.execute(
        "SELECT id, embedding FROM chunks WHERE embedding IS NOT NULL").fetchall(), pool)
    score: dict[int, float] = {}
    for ranking in (keyword, meaning):
        for rank, chunk in enumerate(ranking):
            score[chunk] = score.get(chunk, 0.0) + 1.0 / (60 + rank)
    if not score:
        return [], False
    candidates = {r["id"]: r for r in conn.execute(
        "SELECT c.id,c.ref,c.title,c.text,f.valid_from,f.valid_until,f.confirmed,f.source,f.note "
        "FROM json_each(?) ids JOIN chunks c ON c.id=ids.value "
        "LEFT JOIN facts f ON f.subject=c.page AND c.ref=f.subject || '#fact-' || f.id",
        (json.dumps(list(score)),))}
    best: dict[str, dict] = {}
    for chunk in sorted(score, key=score.get, reverse=True):
        row = candidates.get(chunk)
        if row and row["ref"] not in best:
            fact = {k: row[k] for k in ("valid_from", "valid_until", "confirmed", "source", "note")} \
                if row["valid_from"] is not None else None
            best[row["ref"]] = {"id": row["ref"], "title": row["title"], "text": row["text"],
                                "fact": fact}
    found = list(best.values())
    return found[offset:offset + limit], len(found) > offset + limit


def suggest(conn: sqlite3.Connection, wanted: str, limit: int = 3) -> list[str]:
    """Page ids that share a word with `wanted` (for "did you mean" on a miss)."""
    words = [w for w in re.findall(r"[A-Za-z0-9]+", wanted.split(":", 1)[-1]) if len(w) > 1]
    if not words:
        return []
    where = " OR ".join("id LIKE ?" for _ in words)
    rows = conn.execute(f"SELECT id FROM pages WHERE {where} ORDER BY length(id) LIMIT ?",
                        [f"%{w}%" for w in words] + [limit])
    return [r[0] for r in rows]


_READ = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "data_version"}  # FTS5 reads data_version
CELL_MAX = 2000
RESULT_MAX = 100_000  # characters across all returned cells


def _read_auth(action: int, arg1, arg2, db, source) -> int:
    if action in _READ or (action == sqlite3.SQLITE_PRAGMA and arg1 in _PRAGMAS):
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
    with connect():  # make sure the database exists
        pass
    conn = sqlite3.connect(config.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=5, isolation_level=None)
    try:
        conn.execute("PRAGMA query_only = 1")
        _functions(conn)
        conn.set_authorizer(_read_auth)
        deadline = time.monotonic() + config.SQL_TIMEOUT_SECONDS
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)
        try:
            cur = conn.execute(sql, params or [])
            if cur.description is None:
                raise ValueError("only queries (SELECT, WITH) are allowed here; change rows with `write`")
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
                raise ValueError("not allowed: `sql` is read-only (one SELECT or WITH); change rows with `write`") \
                    from None
            raise ValueError(f"SQL error: {e}") from None
        except (sqlite3.ProgrammingError, sqlite3.DatabaseError) as e:
            if "not authorized" in str(e):
                raise ValueError("not allowed: `sql` is read-only (one SELECT or WITH); change rows with `write`") \
                    from None
            raise ValueError(f"SQL error: {e}") from None
    finally:
        conn.close()
