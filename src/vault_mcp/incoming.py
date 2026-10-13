"""Incoming files: upload tickets, streamed PUTs and server-side fetches.

Everything lands in .incoming/ inside the vault mount (so filing is a rename) and is
filed into a blob + file note as soon as it is complete.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import time
import urllib.parse
import urllib.request
from collections.abc import AsyncIterator
from pathlib import Path

from . import config
from .documents import DocumentError, safe_name, walk


def _root() -> Path:
    root = config.incoming_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def open_ticket(client: str, purpose: str, tags: list[str] | None) -> str:
    """An upload ticket: what the files are for, who brings them. Its id goes into the signed PUT link."""
    slug = re.sub(r"[^a-z0-9]+", "-", client.lower()).strip("-")[:24] or "agent"
    ticket = f"{slug}-{secrets.token_hex(6)}"
    (_root() / f"{ticket}.json").write_text(json.dumps(
        {"client": client, "purpose": purpose, "tags": tags or [], "created": int(time.time())}))
    return ticket


def ticket(ticket_id: str) -> dict | None:
    f = _root() / f"{ticket_id}.json"
    return json.loads(f.read_text()) if f.is_file() else None


def _part() -> Path:
    return _root() / f"{secrets.token_hex(8)}.part"


def usage() -> int:
    return sum(p.stat().st_size for p in walk(config.blobs_root()))


def check_quota(incoming: int = 0) -> None:
    if usage() + incoming > config.QUOTA_BYTES:
        raise DocumentError(f"stored files would exceed the {config.QUOTA_BYTES // 1024**3} GB quota")


async def receive(chunks: AsyncIterator[bytes], limit: int) -> Path:
    """Stream a request body to a partial file; the caller files it and the part disappears."""
    part, size = _part(), 0
    try:
        with part.open("wb") as out:
            async for chunk in chunks:
                size += len(chunk)
                if size > limit:
                    raise DocumentError(f"file is larger than {limit // 1024**2} MB")
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return part


# --- fetching: public addresses only -------------------------------------------------

def _check_public(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise DocumentError("only http(s) URLs can be fetched")
    for info in socket.getaddrinfo(parts.hostname, parts.port or 443, proto=socket.IPPROTO_TCP):
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise DocumentError(f"{parts.hostname} resolves to a non-public address")


class _PublicRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url: str, name: str | None = None) -> tuple[Path, str]:
    """Download a public URL to a partial file. Returns (part, file name)."""
    _check_public(url)
    opener = urllib.request.build_opener(_PublicRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": "vault-mcp"})
    part, size = _part(), 0
    try:
        with opener.open(request, timeout=60) as resp, part.open("wb") as out:
            if not name:
                m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", resp.headers.get("Content-Disposition", ""))
                name = m.group(1) if m else urllib.parse.urlsplit(resp.geturl()).path.rsplit("/", 1)[-1]
            while chunk := resp.read(1 << 20):
                size += len(chunk)
                if size > config.FETCH_MAX_BYTES:
                    raise DocumentError(f"file is larger than {config.FETCH_MAX_BYTES // 1024**2} MB")
                out.write(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return part, safe_name(name or "file")


def prune(now: float) -> None:
    """Expired tickets and abandoned partial files."""
    for f in _root().iterdir():
        age = now - f.stat().st_mtime
        if (f.suffix == ".json" and age > config.LINK_TTL + 3600) or (f.suffix == ".part" and age > 86400):
            f.unlink(missing_ok=True)
