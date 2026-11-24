"""SQLite store for Vault: two-DB split (vault.db + rebuildable int8 vectors.db)."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from . import config, documents, embed, signing

CELL_MAX = 2000
RESULT_MAX = 100_000
ROW_LIMIT_MAX = 100

SQL_DIR = Path(__file__).resolve().parent / "sql"
MIGRATIONS_DIR = SQL_DIR / "migrations"
VEC_SCHEMA_PATH = SQL_DIR / "vectors.sql"
BUILTIN_VIEWS_DIR = SQL_DIR / "views"

_ALLOW_ACTIONS = {
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_INSERT,
    sqlite3.SQLITE_UPDATE,
    sqlite3.SQLITE_DELETE,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
    sqlite3.SQLITE_TRANSACTION,
}
_WRITE_ACTIONS = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}
_SAFE_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info", "data_version", "foreign_keys"}
_ATTACH_RELS = ("requires", "contradicts", "part_of", "relates")
_OPAQUE_DELIM_RE = re.compile(r"\b[A-Za-z0-9]+(?:[-_./:][A-Za-z0-9]+)+\b")
_OPAQUE_ALNUM_RE = re.compile(r"\b(?=[A-Za-z0-9]*[0-9])(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{4,}\b")
_VEC_SYNC_LOCK = threading.Lock()


def _authorizer(action: int, arg1: Any, arg2: Any, db_name: Any, source: Any) -> int:
    if action in _ALLOW_ACTIONS:
        if action in _WRITE_ACTIONS and (
            db_name == "vec" or arg1 in ("sqlite_schema", "sqlite_master")
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and arg1 in _SAFE_PRAGMAS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def register_functions(conn: sqlite3.Connection) -> None:
    """Register `embed(text)` and `cosine(a, b)` UDFs on the SQLite connection."""
    conn.create_function("embed", 1, embed.query, deterministic=True)
    conn.create_function("cosine", 2, embed.cosine, deterministic=True)


def _configure_conn(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -16000")


def connect(
    db_path: Path | str | None = None,
    *,
    vec_db_path: Path | str | None = None,
    attach_vectors: bool = False,
) -> sqlite3.Connection:
    """Open WAL connection with foreign_keys=ON, busy_timeout=5000, and vector UDFs."""
    target = Path(db_path) if db_path is not None else config.DB_PATH
    conn = sqlite3.connect(str(target), timeout=5.0, isolation_level=None)
    _configure_conn(conn)
    register_functions(conn)
    if attach_vectors:
        vpath = config.vec_db_path(target) if vec_db_path is None else Path(vec_db_path)
        if vpath.exists():
            conn.execute("ATTACH DATABASE ? AS vec", (str(vpath),))
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> int:
    """Apply numbered forward-only SQL migrations (`001-*.sql`, ...) tracked by PRAGMA user_version."""
    mdir = migrations_dir or MIGRATIONS_DIR
    current = int(conn.execute("PRAGMA user_version").fetchone()[0])
    files = sorted(p for p in mdir.glob("[0-9][0-9][0-9]-*.sql") if p.is_file())
    for path in files:
        ver = int(path.name.split("-", 1)[0])
        if ver <= current:
            continue
        sql_text = path.read_text(encoding="utf-8")
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.executescript(f"BEGIN;\n{sql_text}\nPRAGMA user_version = {ver};\nCOMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("PRAGMA foreign_keys = ON")
        current = ver
    return current


def _apply_view_dir(conn: sqlite3.Connection, vdir: Path) -> None:
    if not vdir.is_dir():
        return
    for sql_file in sorted(vdir.glob("*.sql")):
        conn.executescript(sql_file.read_text(encoding="utf-8"))


def reload_views(db_path: Path | str | None = None, views_dir: Path | None = None) -> None:
    """Reload packaged SQL views and overlay userspace views from `$VAULT_ROOT/usr/views/*.sql`."""
    target = Path(db_path) if db_path is not None else config.DB_PATH
    vdir = views_dir if views_dir is not None else config.VIEWS_DIR
    with contextlib.closing(connect(target)) as conn:
        _apply_view_dir(conn, BUILTIN_VIEWS_DIR)
        if vdir.resolve() != BUILTIN_VIEWS_DIR.resolve():
            _apply_view_dir(conn, vdir)
        conn.execute("PRAGMA foreign_keys = ON")


def init_db(
    db_path: Path | str | None = None,
    *,
    migrations_dir: Path | None = None,
    views_dir: Path | None = None,
    vec_db_path: Path | str | None = None,
    vec_schema_path: Path | None = None,
) -> int:
    """Initialize canonical vault.db migrations + views and rebuildable vectors.db."""
    db_file = Path(db_path) if db_path is not None else config.DB_PATH
    db_file.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(connect(db_file)) as conn:
        version = apply_migrations(conn, migrations_dir=migrations_dir)
    reload_views(db_file, views_dir=views_dir)

    vpath = config.vec_db_path(db_path) if vec_db_path is None else Path(vec_db_path)
    vpath.parent.mkdir(parents=True, exist_ok=True)
    vec_schema = (vec_schema_path or VEC_SCHEMA_PATH).read_text(encoding="utf-8")
    with contextlib.closing(sqlite3.connect(str(vpath), timeout=5.0, isolation_level=None)) as vc:
        vc.execute("PRAGMA journal_mode = WAL")
        vc.execute("PRAGMA synchronous = NORMAL")
        vc.execute("PRAGMA busy_timeout = 5000")
        vc.executescript(vec_schema)
    return version


def schema_text() -> str:
    """Return canonical DDL across migrations and vectors schema."""
    parts = [p.read_text(encoding="utf-8") for p in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9]-*.sql"))]
    parts.append(VEC_SCHEMA_PATH.read_text(encoding="utf-8"))
    return "\n\n".join(parts)


def views_text(db_path: Path | str | None = None) -> str:
    """Return currently active SQL view definitions from vault.db."""
    with contextlib.closing(connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view' ORDER BY name"
        ).fetchall()
    return "\n\n".join(f"-- {r['name']}\n{r['sql']};" for r in rows if r["sql"])


def _content_digest(parts: tuple[str, ...]) -> bytes:
    h = hashlib.blake2b(digest_size=16)
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.digest()


def _sync_vectors_conn(conn: sqlite3.Connection, vec_conn: sqlite3.Connection) -> int:
    """Delta-sync int8 embeddings in `vec_conn` from `conn` using blake2b content_hash."""
    model = embed.model_id()
    written = 0

    existing_items = {
        r["item_id"]: (r["model"], bytes(r["content_hash"]))
        for r in vec_conn.execute("SELECT item_id, model, content_hash FROM items_vec").fetchall()
    }
    live_items = conn.execute("SELECT id, kind, title, body FROM items").fetchall()
    live_item_ids: set[str] = set()
    dirty_items: list[tuple[str, bytes, bytes]] = []

    for r in live_items:
        iid = r["id"]
        live_item_ids.add(iid)
        digest = _content_digest((r["kind"], r["title"], r["body"]))
        if existing_items.get(iid) != (model, digest):
            blob = embed.passage_int8(f"{r['kind']} {r['title']}", r["body"])
            dirty_items.append((iid, digest, blob))

    stale_items = [iid for iid in existing_items if iid not in live_item_ids]

    existing_eps = {
        r["episode_id"]: (r["model"], bytes(r["content_hash"]))
        for r in vec_conn.execute("SELECT episode_id, model, content_hash FROM episodes_vec").fetchall()
    }
    live_eps = conn.execute("SELECT id, source, kind, context, detail FROM episodes").fetchall()
    live_ep_ids: set[int] = set()
    dirty_eps: list[tuple[int, bytes, bytes]] = []

    for r in live_eps:
        eid = int(r["id"])
        live_ep_ids.add(eid)
        digest = _content_digest((r["source"], r["kind"], r["context"], r["detail"]))
        if existing_eps.get(eid) != (model, digest):
            blob = embed.passage_int8(f"{r['source']} {r['kind']} {r['context']}", r["detail"])
            dirty_eps.append((eid, digest, blob))

    stale_eps = [eid for eid in existing_eps if eid not in live_ep_ids]

    if stale_items or dirty_items or stale_eps or dirty_eps:
        vec_conn.execute("BEGIN")
        try:
            if stale_items:
                vec_conn.executemany("DELETE FROM items_vec WHERE item_id = ?", [(i,) for i in stale_items])
                written += len(stale_items)
            if dirty_items:
                vec_conn.executemany(
                    """
                    INSERT INTO items_vec(item_id, model, content_hash, embedding)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(item_id) DO UPDATE SET
                      model = excluded.model,
                      content_hash = excluded.content_hash,
                      embedding = excluded.embedding
                    """,
                    [(iid, model, digest, blob) for iid, digest, blob in dirty_items],
                )
                written += len(dirty_items)
            if stale_eps:
                vec_conn.executemany("DELETE FROM episodes_vec WHERE episode_id = ?", [(e,) for e in stale_eps])
                written += len(stale_eps)
            if dirty_eps:
                vec_conn.executemany(
                    """
                    INSERT INTO episodes_vec(episode_id, model, content_hash, embedding)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(episode_id) DO UPDATE SET
                      model = excluded.model,
                      content_hash = excluded.content_hash,
                      embedding = excluded.embedding
                    """,
                    [(eid, model, digest, blob) for eid, digest, blob in dirty_eps],
                )
                written += len(dirty_eps)
            vec_conn.execute("COMMIT")
        except Exception:
            if vec_conn.in_transaction:
                vec_conn.execute("ROLLBACK")
            raise
    return written


def sync_vectors(db_path: Path | str | None = None, vec_db_path: Path | str | None = None) -> int:
    """Delta-sync `vectors.db` (`items_vec` & `episodes_vec`) against `vault.db`."""
    target = Path(db_path) if db_path is not None else config.DB_PATH
    vpath = config.vec_db_path(db_path) if vec_db_path is None else Path(vec_db_path)
    with _VEC_SYNC_LOCK:
        if not vpath.exists():
            vec_schema = VEC_SCHEMA_PATH.read_text(encoding="utf-8")
            vpath.parent.mkdir(parents=True, exist_ok=True)
            with contextlib.closing(sqlite3.connect(str(vpath), timeout=5.0, isolation_level=None)) as vc:
                vc.execute("PRAGMA journal_mode = WAL")
                vc.executescript(vec_schema)
        with (
            contextlib.closing(connect(target)) as conn,
            contextlib.closing(sqlite3.connect(str(vpath), timeout=5.0, isolation_level=None)) as vec_conn,
        ):
            vec_conn.row_factory = sqlite3.Row
            vec_conn.execute("PRAGMA journal_mode = WAL")
            vec_conn.execute("PRAGMA synchronous = NORMAL")
            vec_conn.execute("PRAGMA busy_timeout = 5000")
            return _sync_vectors_conn(conn, vec_conn)


def rebuild_vectors(
    db_path: Path | str | None = None,
    vec_db_path: Path | str | None = None,
    *,
    vec_schema_path: Path | None = None,
) -> int:
    """Drop and recreate rebuildable `vectors.db` completely from canonical `vault.db`."""
    target = Path(db_path) if db_path is not None else config.DB_PATH
    vpath = config.vec_db_path(db_path) if vec_db_path is None else Path(vec_db_path)
    vpath.parent.mkdir(parents=True, exist_ok=True)
    vec_schema = (vec_schema_path or VEC_SCHEMA_PATH).read_text(encoding="utf-8")
    with _VEC_SYNC_LOCK:
        with (
            contextlib.closing(connect(target)) as conn,
            contextlib.closing(sqlite3.connect(str(vpath), timeout=5.0, isolation_level=None)) as vec_conn,
        ):
            vec_conn.row_factory = sqlite3.Row
            vec_conn.execute("PRAGMA journal_mode = WAL")
            vec_conn.execute("PRAGMA synchronous = NORMAL")
            vec_conn.execute("PRAGMA busy_timeout = 5000")
            vec_conn.executescript(
                "DROP TABLE IF EXISTS items_vec; DROP TABLE IF EXISTS episodes_vec;\n" + vec_schema
            )
            return _sync_vectors_conn(conn, vec_conn)


def rebuild_cli() -> None:
    """CLI entrypoint (`vault-rebuild-vectors`) for rebuilding `vectors.db`."""
    init_db()
    count = rebuild_vectors()
    docs = documents.sync(config.vec_db_path(), force=True)
    print(f"rebuilt vectors.db: {count} rows and {docs} documents encoded ({embed.model_id()})")


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")))
    except Exception as err:
        raise ValueError("invalid pagination cursor") from err


def _clip_cell(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(value)} bytes>"
    if isinstance(value, str) and len(value) > CELL_MAX:
        return value[:CELL_MAX] + f"... [{len(value) - CELL_MAX} more chars]"
    return value


def run_sql(
    db_path: Path | str | None,
    query: str,
    params: list[Any] | dict[str, Any] | None = None,
    limit: int = ROW_LIMIT_MAX,
    cursor: str | None = None,
    *,
    vec_db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Execute one SQL statement with timeout, authorizer (vec read-only), 100-row cap, and delta sync."""
    target = Path(db_path) if db_path is not None else config.DB_PATH
    vpath = config.vec_db_path(db_path) if vec_db_path is None else Path(vec_db_path)
    offset = _decode_cursor(cursor)
    cap = max(1, min(limit, ROW_LIMIT_MAX))

    with contextlib.closing(connect(target, vec_db_path=vpath, attach_vectors=True)) as conn:
        before_changes = conn.total_changes
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + config.SQL_TIMEOUT_SECONDS
        conn.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)

        try:
            cur = conn.execute(query, params if params is not None else [])
        except sqlite3.OperationalError as err:
            msg = str(err)
            if "interrupted" in msg:
                raise ValueError(f"query timed out after {config.SQL_TIMEOUT_SECONDS:g}s") from None
            if "not authorized" in msg or "authorization denied" in msg:
                raise ValueError(f"SQL operation not permitted: {msg}") from None
            raise ValueError(f"SQL error: {msg}") from None
        except (sqlite3.IntegrityError, sqlite3.ProgrammingError, sqlite3.DatabaseError) as err:
            raise ValueError(f"SQL error: {err}") from None
        finally:
            conn.set_progress_handler(None, 0)
            conn.set_authorizer(None)

        if cur.description is None:
            changed = conn.total_changes - before_changes
            rows_affected = cur.rowcount if cur.rowcount >= 0 else changed
            cur.close()
            vec_synced = 0
            if changed > 0:
                if vpath.exists():
                    conn.execute("DETACH DATABASE vec")
                vec_synced = sync_vectors(target, vec_db_path=vpath)
            return {
                "columns": [],
                "rows": [],
                "rows_affected": rows_affected,
                "vectors_synced": vec_synced,
                "truncated": False,
                "next_cursor": None,
            }

        columns = [d[0] for d in cur.description]
        for _ in range(offset):
            if cur.fetchone() is None:
                break

        rows: list[dict[str, Any]] = []
        total_chars = 0
        more = False
        for raw in cur.fetchmany(cap + 1):
            row = {col: _clip_cell(val) for col, val in zip(columns, raw, strict=True)}
            row_chars = sum(len(str(v)) for v in row.values())
            if len(rows) == cap or (rows and total_chars + row_chars > RESULT_MAX):
                more = True
                break
            rows.append(row)
            total_chars += row_chars

        changed = conn.total_changes - before_changes
        cur.close()
        if changed > 0:
            if vpath.exists():
                conn.execute("DETACH DATABASE vec")
            sync_vectors(target, vec_db_path=vpath)

        return {
            "columns": columns,
            "rows": rows,
            "truncated": more,
            "next_cursor": _encode_cursor(offset + len(rows)) if more else None,
        }


def _estimate_tokens(obj: Any) -> int:
    return max(1, len(json.dumps(obj, ensure_ascii=True)) // 4)


def _opaque_tokens(query_text: str) -> list[str]:
    seen: list[str] = []
    for rx in (_OPAQUE_DELIM_RE, _OPAQUE_ALNUM_RE):
        for tok in rx.findall(query_text):
            if tok not in seen:
                seen.append(tok)
    return seen


def _like_pattern(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


DOC_MIN_SIMILARITY = 0.45


def _document_hits(vc: sqlite3.Connection, q_vec: bytes | None, k: int) -> list[dict[str, Any]]:
    """Best document passages for a query, at most one per document, with a download link."""
    if vc.execute("SELECT 1 FROM sqlite_master WHERE name = 'doc_chunks'").fetchone() is None:
        return []
    rows = [((r["path"], r["chunk"]), r["embedding"])
            for r in vc.execute("SELECT path, chunk, embedding FROM doc_chunks").fetchall()]
    hits: list[dict[str, Any]] = []
    seen: set[str] = set()
    for (path, chunk), sim in embed.nearest_int8(q_vec, rows, 4 * k):
        if sim < DOC_MIN_SIMILARITY or path in seen:
            continue
        seen.add(path)
        meta = vc.execute("SELECT name, mime FROM documents WHERE path = ?", (path,)).fetchone()
        text = vc.execute("SELECT text FROM doc_chunks WHERE path = ? AND chunk = ?", (path, chunk)).fetchone()
        try:
            url = signing.document_link(path)
        except ValueError:
            url = None
        hits.append({"path": path, "name": meta["name"] if meta else path, "mime": meta["mime"] if meta else None,
                     "chunk": chunk, "score": round(sim, 4), "text": text["text"] if text else "", "url": url})
        if len(hits) >= max(1, k // 2):
            break
    return hits


def recall(
    db_path: Path | str | None,
    query_text: str,
    budget: int = 1800,
    k: int = 6,
    *,
    vec_db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Hybrid int8 retrieval across items_vec + episodes_vec, exact/LIKE fallback, and 1-hop attach."""
    if not query_text or not query_text.strip():
        raise ValueError("query must not be empty")
    budget = max(200, int(budget))
    k = max(1, min(int(k), 25))
    target = Path(db_path) if db_path is not None else config.DB_PATH
    vpath = config.vec_db_path(db_path) if vec_db_path is None else Path(vec_db_path)

    q_vec = embed.query(query_text)
    terms = set(re.findall(r"[a-z0-9]+", query_text.lower()))
    opaque = _opaque_tokens(query_text)

    with contextlib.closing(connect(target)) as conn:
        active_rows = conn.execute(
            "SELECT id, kind, title, body FROM items WHERE status = 'active'"
        ).fetchall()

        if vpath.exists():
            with contextlib.closing(sqlite3.connect(str(vpath), timeout=5.0, isolation_level=None)) as vc:
                vc.row_factory = sqlite3.Row
                item_vec_rows = [
                    (r["item_id"], r["embedding"])
                    for r in vc.execute("SELECT item_id, embedding FROM items_vec").fetchall()
                ]
                ep_vec_rows = [
                    (r["episode_id"], r["embedding"])
                    for r in vc.execute("SELECT episode_id, embedding FROM episodes_vec").fetchall()
                ]
                doc_hits = _document_hits(vc, q_vec, k)
        else:
            item_vec_rows = []
            ep_vec_rows = []
            doc_hits = []

        item_vec_scores = dict(embed.nearest_int8(q_vec, item_vec_rows, len(item_vec_rows)))
        ep_vec_hits = [
            (int(eid), sim)
            for eid, sim in embed.nearest_int8(q_vec, ep_vec_rows, max(4, k))
            if sim >= 0.45
        ]

        item_like_hits: set[str] = set()
        ep_like_hits: list[int] = []
        for tok in opaque:
            pat = _like_pattern(tok)
            for r in conn.execute(
                """
                SELECT id FROM items
                WHERE status = 'active' AND (body LIKE ? ESCAPE '\\' OR title LIKE ? ESCAPE '\\')
                """,
                (pat, pat),
            ).fetchall():
                item_like_hits.add(r["id"])
            for r in conn.execute(
                """
                SELECT id FROM episodes
                WHERE context LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\'
                ORDER BY id DESC LIMIT 6
                """,
                (pat, pat),
            ).fetchall():
                eid = int(r["id"])
                if eid not in ep_like_hits:
                    ep_like_hits.append(eid)

        item_meta: dict[str, dict[str, Any]] = {}
        ranked: list[tuple[float, str, dict[str, float]]] = []
        for r in active_rows:
            item_id = r["id"]
            item_meta[item_id] = {
                "id": item_id,
                "kind": r["kind"],
                "title": r["title"],
                "body": r["body"],
            }
            v_sim = round(max(0.0, item_vec_scores.get(item_id, 0.0)) * 1.65, 4)
            slug_tokens = set(re.findall(r"[a-z0-9]+", item_id.lower())) | set(
                re.findall(r"[a-z0-9]+", r["title"].lower())
            )
            exact = 0.65 if terms & slug_tokens else 0.0
            opaque_boost = 2.0 if item_id in item_like_hits else 0.0
            total = round(v_sim + exact + opaque_boost, 4)
            ranked.append((total, item_id, {"vec": v_sim, "exact": exact, "opaque": opaque_boost, "total": total}))

        ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
        top_items_full: list[dict[str, Any]] = [
            {**item_meta[item_id], "score": score}
            for total, item_id, score in ranked[:k]
            if total > 0.0
        ]
        top_set = {it["id"] for it in top_items_full}

        direct_ep_ids: list[int] = list(ep_like_hits)
        for eid, _ in ep_vec_hits:
            if eid not in direct_ep_ids:
                direct_ep_ids.append(eid)

        if direct_ep_ids:
            ph = ",".join("?" for _ in direct_ep_ids)
            for b in conn.execute(
                f"SELECT DISTINCT item_id FROM episode_items WHERE episode_id IN ({ph})",
                tuple(direct_ep_ids),
            ).fetchall():
                iid = b["item_id"]
                if iid in item_meta and iid not in top_set:
                    top_set.add(iid)
                    top_items_full.append(
                        {
                            **item_meta[iid],
                            "via": "episodes_vec",
                            "score": {"vec": 0.0, "exact": 0.0, "opaque": 0.0, "total": 0.5},
                        }
                    )

        attached_links: list[dict[str, Any]] = []
        attached_ids: set[str] = set()
        if top_set:
            ph_ids = ",".join("?" for _ in top_set)
            ph_rels = ",".join("?" for _ in _ATTACH_RELS)
            for link in conn.execute(
                f"""
                SELECT src, rel, dst, weight
                FROM links
                WHERE status = 'active'
                  AND rel IN ({ph_rels})
                  AND (src IN ({ph_ids}) OR dst IN ({ph_ids}))
                ORDER BY weight DESC, src ASC, dst ASC
                LIMIT 12
                """,
                (*_ATTACH_RELS, *tuple(top_set), *tuple(top_set)),
            ).fetchall():
                attached_links.append(
                    {"src": link["src"], "rel": link["rel"], "dst": link["dst"], "weight": float(link["weight"])}
                )
                attached_ids.update((link["src"], link["dst"]))

        neighbor_ids = sorted(iid for iid in attached_ids if iid in item_meta and iid not in top_set)
        attached_items_full = [item_meta[iid] for iid in neighbor_ids]

        due_time_cues = conn.execute("SELECT id, item_id, due_at, action FROM v_due_cues").fetchall()
        cues_fired = [
            {
                "id": c["id"],
                "trigger_kind": "time",
                "item_id": c["item_id"],
                "due_at": c["due_at"],
                "action": c["action"],
            }
            for c in due_time_cues
        ]
        cue_targets = top_set | set(neighbor_ids)
        if cue_targets:
            ph_cue = ",".join("?" for _ in cue_targets)
            for c in conn.execute(
                f"""
                SELECT id, item_id, on_item_id, action FROM cues
                WHERE status = 'armed' AND trigger_kind = 'event' AND on_item_id IN ({ph_cue})
                """,
                tuple(cue_targets),
            ).fetchall():
                cues_fired.append(
                    {
                        "id": c["id"],
                        "trigger_kind": "event",
                        "item_id": c["item_id"],
                        "on_item_id": c["on_item_id"],
                        "action": c["action"],
                    }
                )

        episodes_by_id: dict[int, dict[str, Any]] = {}
        ordered_ep_ids: list[int] = []
        if direct_ep_ids:
            ph_ep = ",".join("?" for _ in direct_ep_ids)
            by_id = {
                int(r["id"]): dict(r)
                for r in conn.execute(
                    f"""
                    SELECT id, at, kind, source, context, detail, confidence, uri
                    FROM episodes WHERE id IN ({ph_ep})
                    """,
                    tuple(direct_ep_ids),
                ).fetchall()
            }
            for eid in direct_ep_ids:
                if eid in by_id:
                    episodes_by_id[eid] = by_id[eid]
                    ordered_ep_ids.append(eid)

        if top_set:
            ph_top = ",".join("?" for _ in top_set)
            for r in conn.execute(
                f"""
                SELECT DISTINCT e.id, e.at, e.kind, e.source, e.context, e.detail, e.confidence, e.uri
                FROM episodes e
                JOIN episode_items ei ON ei.episode_id = e.id
                WHERE ei.item_id IN ({ph_top})
                ORDER BY e.at DESC, e.id DESC
                LIMIT ?
                """,
                (*tuple(top_set), max(3, k)),
            ).fetchall():
                eid = int(r["id"])
                if eid not in episodes_by_id:
                    episodes_by_id[eid] = dict(r)
                    ordered_ep_ids.append(eid)

        episodes_full = [episodes_by_id[eid] for eid in ordered_ep_ids]

    packet: dict[str, Any] = {
        "query": query_text,
        "cues_fired": cues_fired,
        "attached_links": attached_links,
        "items": [],
        "attached_items": [],
        "episodes": [],
        "documents": [],
    }
    used = _estimate_tokens(packet)
    for item in top_items_full:
        cost = _estimate_tokens(item)
        if used + cost <= budget or not packet["items"]:
            packet["items"].append(item)
            used += cost
    for item in attached_items_full:
        cost = _estimate_tokens(item)
        if used + cost <= budget:
            packet["attached_items"].append(item)
            used += cost
    for ep in episodes_full:
        cost = _estimate_tokens(ep)
        if used + cost <= budget:
            packet["episodes"].append(ep)
            used += cost
    for doc in doc_hits:
        cost = _estimate_tokens(doc)
        if used + cost <= budget:
            packet["documents"].append(doc)
            used += cost
    packet["approx_tokens"] = used
    return packet
