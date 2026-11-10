"""Filing: every incoming file becomes an immutable blob plus one page that describes it.

    blobs/<sha256[:16]>.<ext>   the bytes, named by content, never renamed (not in git)
    pages  files/<slug>         what the file is; `files` holds blob, hash, type, size and earlier versions

Uploads (signed PUTs), URL fetches, ChatGPT attachments and the drop folder all go through file().
A new version of a file replaces the blob of its page; the earlier blobs stay, listed in `versions`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import unicodedata
from pathlib import Path

from . import config, documents, incoming, repo, store


def origin_of(client: str) -> str:
    """Which agent family a client name belongs to (claude-ai, openai-mcp, hermes...)."""
    c = client.lower()
    for key, origin in (("claude", "claude"), ("chatgpt", "chatgpt"), ("openai", "chatgpt"), ("hermes", "hermes")):
        if key in c:
            return origin
    return "manual"


def slugify(text: str, max_len: int = 80) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().replace("'", "")
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug if len(slug) <= max_len else slug[:max_len].rsplit("-", 1)[0]


def _extension(name: str) -> str:
    ext = Path(name).suffix.lower()
    return ext if re.fullmatch(r"\.[a-z0-9]{1,10}", ext) else ""


def store_blob(src: Path, sha: str, name: str) -> str:
    """Move src into the blob store under its content hash; returns the vault-relative path."""
    root = config.blobs_root()
    root.mkdir(parents=True, exist_ok=True)
    for length in (16, 32, 64):  # a longer name only if a 64-bit prefix ever collides
        dest = root / f"{sha[:length]}{_extension(name)}"
        if not dest.exists():
            incoming.check_quota(src.stat().st_size)  # actual bytes, under the shared writer lock
            shutil.move(src, dest)  # a rename: incoming files live on the same mount
            os.chmod(dest, 0o664)
            return f"blobs/{dest.name}"
        if documents.sha256_of(dest) == sha:
            src.unlink()
            return f"blobs/{dest.name}"
    raise RuntimeError(f"hash collision storing {name}")


def file(src: Path, *, original_name: str, origin: str, added_by: str, purpose: str = "",
         url: str | None = None, tags: list[str] | None = None, replaces: str | None = None) -> dict:
    """File src (consumed) into the vault. Returns {"note", "blob", "duplicate"}.
    With replaces (a file page, files/<name>), src becomes that page's new version instead."""
    sha, size = documents.sha256_of(src), src.stat().st_size
    mime = documents.mime_of(original_name)
    with repo.locked(), store.connect() as conn:
        if replaces:
            old = conn.execute("SELECT * FROM files WHERE id = ?", (replaces,)).fetchone()
            if old is None:
                src.unlink()
                raise documents.DocumentError(f"no file page `{replaces}` to replace")
            if old["sha256"] == sha:
                src.unlink()
                return {"note": replaces, "blob": None, "duplicate": True}
            blob = store_blob(src, sha, original_name)
            versions = [b for b in dict.fromkeys([old["blob"], *json.loads(old["versions"])]) if b != blob]
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE files SET blob = ?, sha256 = ?, mime = ?, size = ?, name = ?, versions = ? "
                         "WHERE id = ?", (blob, sha, mime, size, original_name, json.dumps(versions), replaces))
            conn.execute(f"UPDATE pages SET updated = {store.NOW} WHERE id = ?", (replaces,))
            note, message = replaces, f"file: new version of {replaces} ({added_by})"
        else:
            if existing := conn.execute("SELECT id FROM files WHERE sha256 = ?", (sha,)).fetchone():
                src.unlink()
                return {"note": existing["id"], "blob": None, "duplicate": True}
            blob = store_blob(src, sha, original_name)
            today = dt.date.today().isoformat()
            size_label = f"{size / 1024**2:.1f} MB" if size >= 1024**2 else f"{size / 1024:.0f} KB"
            about = purpose.strip() or f"{original_name} ({documents.media_of(original_name)}, {size_label}), " \
                                       "not described yet."
            body = (f"{about}\n\nAdded by {added_by} ({origin}) on {today}"
                    + (f" from {url}" if url else "") + "."
                    + (f"\n\nTags: {', '.join(sorted(set(tags)))}" if tags else ""))
            base = f"files/{slugify(Path(original_name).stem, 60) or 'file'}"
            note, n = base, 2
            while conn.execute("SELECT 1 FROM pages WHERE id = ?", (note,)).fetchone():
                note, n = f"{base}-{n}", n + 1
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO pages (id, title, body) VALUES (?, ?, ?)", (note, original_name, body))
            conn.execute("INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, '[]')", (note, blob, sha, mime, size,
                                                                                original_name))
            message = f"file: {original_name} ({added_by})"
        conn.execute("INSERT INTO log (actor, message, changed) VALUES (?, ?, ?)",
                     (added_by, message, json.dumps([note])))
        conn.execute("COMMIT")
        store.sync_blobs(conn)
        sha_commit = store.publish(conn, message, "vault-worker")
        conn.execute("UPDATE log SET sha = ? WHERE id = (SELECT max(id) FROM log)", (sha_commit,))
    return {"note": note, "blob": blob, "duplicate": False}
