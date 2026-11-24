"""Filesystem gate jailed strictly inside $VAULT_ROOT/; blobs/ is written only by uploads.py."""
from __future__ import annotations

from pathlib import Path

DEFAULT_READ_LIMIT = 20_000


def resolve_in_vault(vault_root: Path, rel_path: str) -> tuple[Path, str]:
    """Resolve `rel_path` strictly inside `vault_root` and return `(abs_path, posix_rel)`."""
    root = vault_root.resolve()
    cleaned = rel_path.strip().lstrip("/")
    if not cleaned:
        raise ValueError("path must not be empty")
    target = (root / cleaned).resolve()
    try:
        rel = target.relative_to(root).as_posix()
    except ValueError as err:
        raise ValueError(f"path escapes VAULT_ROOT: {rel_path}") from err
    return target, rel


def _require_writable(rel: str) -> None:
    if rel == "backups" or rel.startswith("backups/"):
        raise ValueError("backups/ is host-managed and read-only over MCP")
    if not rel.startswith(("notes/", "usr/")):
        raise ValueError("direct file writes are allowed only under notes/ and usr/")


def read_file(vault_root: Path, rel_path: str, offset: int = 0, limit: int = DEFAULT_READ_LIMIT) -> dict[str, object]:
    """Read text files paginated, or return metadata without dumping raw binary bytes."""
    target, rel = resolve_in_vault(vault_root, rel_path)
    if not target.is_file():
        raise FileNotFoundError(f"no such file: {rel}")
    size = target.stat().st_size
    if rel.startswith("blobs/") and target.suffix.lower() not in (".md", ".txt", ".json", ".sql", ".html"):
        return {"path": rel, "binary": True, "size": size, "truncated": False}
    text = target.read_text(encoding="utf-8")
    start = max(0, offset)
    end = start + max(1, limit)
    chunk = text[start:end]
    return {
        "path": rel,
        "binary": False,
        "size": size,
        "offset": start,
        "next_offset": end if end < len(text) else None,
        "truncated": end < len(text),
        "text": chunk,
    }


def write_file(vault_root: Path, rel_path: str, content: str) -> dict[str, object]:
    """Write a UTF-8 file atomically inside notes/ or usr/."""
    target, rel = resolve_in_vault(vault_root, rel_path)
    _require_writable(rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(target)
    return {"path": rel, "size": len(content.encode("utf-8"))}


def delete_file(vault_root: Path, rel_path: str) -> dict[str, object]:
    """Remove a file under notes/ or usr/ (never backups/ or blobs/)."""
    target, rel = resolve_in_vault(vault_root, rel_path)
    _require_writable(rel)
    if not target.exists():
        raise FileNotFoundError(f"no such file: {rel}")
    target.unlink()
    return {"path": rel, "deleted": True}
