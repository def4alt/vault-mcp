"""Filing: every incoming file becomes an immutable blob plus one file note in the graph.

    blobs/<sha256[:16]>.<ext>   the bytes, named by content, never renamed (not in git)
    files/<slug>.md             frontmatter (tags, categories, about, related...) + summary + embed

Uploads (signed PUTs), URL fetches, ChatGPT attachments and the drop folder all go through file().
"""
from __future__ import annotations

import datetime as dt
import os
import re
import shutil
from pathlib import Path

from . import config, documents, index, repo, rules
from .vault import render, slugify, split_frontmatter, unique_path


def origin_of(client: str) -> str:
    """Which agent family a client name belongs to (claude-ai, openai-mcp, hermes...)."""
    c = client.lower()
    for key, origin in (("claude", "claude"), ("chatgpt", "chatgpt"), ("openai", "chatgpt"), ("hermes", "hermes")):
        if key in c:
            return origin
    return "manual"


def note_for_hash(sha: str) -> str | None:
    """The file note for a blob, read straight from files/ so it is never stale."""
    for f in sorted((config.VAULT_ROOT / "files").glob("*.md")):
        meta, _ = split_frontmatter(f.read_text("utf-8", "replace"))
        if meta.get("sha256") == sha:
            return f"files/{f.stem}"
    return None


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
            shutil.move(src, dest)  # a rename: incoming files live on the same mount
            os.chmod(dest, 0o664)
            return f"blobs/{dest.name}"
        if documents.sha256_of(dest) == sha:
            src.unlink()
            return f"blobs/{dest.name}"
    raise RuntimeError(f"hash collision storing {name}")


def file(src: Path, *, original_name: str, origin: str, added_by: str, purpose: str = "",
         url: str | None = None, tags: list[str] | None = None) -> dict:
    """File src (consumed) into the vault. Returns {"note", "blob", "duplicate"}."""
    sha = documents.sha256_of(src)
    if existing := note_for_hash(sha):
        src.unlink()
        return {"note": existing, "blob": None, "duplicate": True}
    size, media, mime = src.stat().st_size, documents.media_of(original_name), documents.mime_of(original_name)
    blob = store_blob(src, sha, original_name)
    text = documents.text_for(config.VAULT_ROOT / blob, sha)
    preview = (text or "")[:800].strip()
    try:
        rules.check_secrets(preview)
    except rules.RuleViolation:
        preview = ""
    today = dt.date.today().isoformat()
    size_label = f"{size / 1024**2:.1f} MB" if size >= 1024**2 else f"{size / 1024:.0f} KB"
    meta = {
        "kind": "file", "as_of": today, "tags": sorted(set(tags or [])) or None, "origin": origin,
        "media": media, "mime": mime, "size": size, "original_name": original_name, "blob": blob,
        "sha256": sha, "url": url,
        "summary": purpose.strip() or f"{original_name} ({media}, {size_label}), not described yet.",
    }
    body = (f"# {original_name}\n\n![[{blob}]]\n\nAdded by {added_by} on {today}."
            + (f"\n\n{purpose.strip()}" if purpose.strip() else "")
            + (f"\n\n## Preview\n\n{preview}" if preview else ""))
    with repo.locked():
        note = unique_path(config.VAULT_ROOT, "files", slugify(Path(original_name).stem, 60) or "file")
        (config.VAULT_ROOT / f"{note}.md").write_text(render(meta, body), "utf-8")
        repo.commit([f"{note}.md"], f"file: {original_name} ({added_by})", "vault-worker")
    index.reindex([f"{note}.md"])
    return {"note": note, "blob": blob, "duplicate": False}
