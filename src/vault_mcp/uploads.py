"""Bringing file bytes in without passing them through a model: signed PUT uploads and server-side fetches.

Bytes stream into $VAULT_ROOT/.incoming/ (ignored by git and Syncthing, same filesystem as blobs/) and are
filed as a content-addressed blob (blobs/<sha256[:16]><ext>) once complete. Recording the file on an item is
the caller's job (`props.blob`), like every other write.
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import secrets
import shlex
import socket
import time
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

from . import config, documents, signing

CHUNK = 1 << 20


def _incoming() -> Path:
    root = config.VAULT_ROOT / ".incoming"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _part() -> Path:
    return _incoming() / f"{secrets.token_hex(8)}.part"


def store_bytes(data: bytes, name: str) -> dict[str, object]:
    """File small inline content (the `put_blob` action) like any upload."""
    part = _part()
    try:
        part.write_bytes(data)
        return file_part(part, name)
    finally:
        part.unlink(missing_ok=True)


def blobs_usage() -> int:
    root = config.VAULT_ROOT / "blobs"
    return sum(p.stat().st_size for p in root.iterdir() if p.is_file()) if root.is_dir() else 0


def check_quota(incoming: int = 0) -> None:
    if blobs_usage() + incoming > config.QUOTA_BYTES:
        raise ValueError(f"stored files would exceed the {config.QUOTA_BYTES // 1024**3} GB quota")


def ext_of(name: str) -> str:
    suffix = Path(name).suffix.lower()
    return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


def file_part(part: Path, name: str) -> dict[str, object]:
    """Move a complete partial file into blobs/ under its content hash; the part is gone afterwards."""
    h = hashlib.sha256()
    size = 0
    with part.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
            size += len(chunk)
    digest = h.hexdigest()
    name = signing.safe_name(name)
    rel = f"blobs/{digest[:16]}{ext_of(name)}"
    target = config.VAULT_ROOT / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    duplicate = target.exists()
    if duplicate:
        part.unlink(missing_ok=True)
    else:
        check_quota(size)
        os.chmod(part, 0o644)
        os.replace(part, target)
    out: dict[str, object] = {"path": rel, "sha256": digest, "size": size, "mime": documents.mime_of(name),
                              "filename": name, "duplicate": duplicate}
    if config.LINK_SECRET:
        out["url"] = signing.blob_link(rel, name)
    return out


def new_upload_link(name: str = "") -> dict[str, object]:
    """A signed PUT URL template; `{filename}` is replaced by each file's name."""
    template, expires = signing.upload_link(secrets.token_hex(12))
    filename = signing.safe_name(name or "file")
    target = template.replace("{filename}", urllib.parse.quote(filename, safe=""))
    return {
        "put_url": template,
        "curl": f"curl -fsS -T {shlex.quote(name or filename)} {shlex.quote(target)}",
        "expires_at": datetime.fromtimestamp(expires, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "max_bytes": config.UPLOAD_MAX_BYTES,
    }


async def receive(chunks: AsyncIterator[bytes], name: str, limit: int | None = None) -> dict[str, object]:
    """Stream a request body into .incoming/, then file it."""
    limit = config.UPLOAD_MAX_BYTES if limit is None else limit
    part, size = _part(), 0
    try:
        with part.open("wb") as out:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise ValueError(f"file is larger than {limit // 1024**2} MB")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        return file_part(part, name)
    finally:
        part.unlink(missing_ok=True)


# --- fetching: public addresses only -------------------------------------------------------------

def _check_public(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("only http(s) URLs can be fetched")
    for info in socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError(f"{parts.hostname} resolves to a non-public address")


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, name: str = "") -> dict[str, object]:
    """Download a public URL (e.g. a chat attachment's download link) and file it."""
    _check_public(url)
    opener = urllib.request.build_opener(_PublicRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": "vault-mcp"})
    part, size = _part(), 0
    try:
        with opener.open(request, timeout=60) as resp, part.open("wb") as out:
            if not name:
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", resp.headers.get("Content-Disposition", ""))
                name = m.group(1) if m else urllib.parse.urlsplit(resp.geturl()).path.rsplit("/", 1)[-1]
            while chunk := resp.read(CHUNK):
                size += len(chunk)
                if size > config.FETCH_MAX_BYTES:
                    raise ValueError(f"file is larger than {config.FETCH_MAX_BYTES // 1024**2} MB")
                out.write(chunk)
        return file_part(part, urllib.parse.unquote(name or "file"))
    finally:
        part.unlink(missing_ok=True)


def prune(max_age_seconds: int = 86400) -> int:
    """Remove abandoned partial uploads."""
    removed = 0
    root = _incoming()
    for p in root.glob("*.part"):
        if time.time() - p.stat().st_mtime > max_age_seconds:
            p.unlink(missing_ok=True)
            removed += 1
    return removed
