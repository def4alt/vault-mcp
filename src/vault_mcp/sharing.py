"""Moving files without the model: signed links (verified by nginx), the inbox, fetching."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import config
from .documents import DocumentError, walk

UPLOAD_ID = re.compile(r"[a-z0-9-]{8,64}")


def safe_name(name: str) -> str:
    name = Path(urllib.parse.unquote(name or "")).name
    name = re.sub(r"[^\w.\- ()]+", "-", name).strip(" .-")
    return (name or "file")[:120]


# --- signed links: nginx secure_link_md5 "$secure_link_expires<uri> <secret>" -------

def _signature(uri: str, expires: int) -> str:
    if not config.LINK_SECRET:
        raise DocumentError("file links are not configured on this server")
    digest = hashlib.md5(f"{expires}{uri} {config.LINK_SECRET}".encode()).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _signed(uri: str, ttl: int | None, suffix: str = "") -> tuple[str, int]:
    expires = int(time.time()) + (ttl or config.LINK_TTL)
    query = urllib.parse.urlencode({"e": expires, "s": _signature(uri, expires)})
    return f"{config.FILES_BASE_URL}{urllib.parse.quote(uri)}{suffix}?{query}", expires


def download_link(rel: str, ttl: int | None = None) -> tuple[str, int]:
    return _signed("/d/" + rel.lstrip("/"), ttl)


def upload_link(upload_id: str, ttl: int | None = None) -> tuple[str, int]:
    return _signed(f"/u/{upload_id}", ttl, suffix="/")


# --- the inbox: one directory per upload or fetch -----------------------------------

def open_upload(client: str, purpose: str, url: str | None = None) -> tuple[str, Path]:
    slug = re.sub(r"[^a-z0-9]+", "-", client.lower()).strip("-")[:24] or "agent"
    upload_id = f"{slug}-{secrets.token_hex(6)}"
    inbox = config.INBOX_ROOT / upload_id
    inbox.mkdir(parents=True, mode=0o2775)
    (inbox / ".meta.json").write_text(json.dumps(
        {"client": client, "purpose": purpose, "url": url, "created": int(time.time())}))
    return upload_id, inbox


def upload_status(upload_id: str) -> dict:
    if not UPLOAD_ID.fullmatch(upload_id):
        raise DocumentError("unknown upload id")
    done = config.INBOX_ROOT / ".done" / f"{upload_id}.json"
    inbox = config.INBOX_ROOT / upload_id
    return {
        "id": upload_id,
        "ingested": json.loads(done.read_text()) if done.exists() else [],
        "waiting": [p.name for p in walk(inbox)] if inbox.exists() else [],
    }


def wait_until_ingested(upload_id: str, seconds: float = 45) -> dict:
    deadline = time.time() + seconds
    while True:
        status = upload_status(upload_id)
        if (status["ingested"] and not status["waiting"]) or time.time() > deadline:
            return status
        time.sleep(1.5)


def usage() -> int:
    roots = (config.DOCS_ROOT / config.SHARED_REL, config.INBOX_ROOT)
    return sum(p.stat().st_size for root in roots for p in walk(root))


def check_quota() -> None:
    if usage() > config.QUOTA_BYTES:
        raise DocumentError(f"the shared space is over its {config.QUOTA_BYTES // 1024**3} GB quota")


# --- fetching: public addresses only (the pod's egress policy enforces it too) ------

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


def fetch(url: str, dest: Path, name: str | None = None) -> Path:
    """Stream a URL into dest; the file appears atomically under its final name."""
    _check_public(url)
    opener = urllib.request.build_opener(_PublicRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": "vault-mcp"})
    with opener.open(request, timeout=60) as resp:
        if not name:
            m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", resp.headers.get("Content-Disposition", ""))
            name = m.group(1) if m else urllib.parse.urlsplit(resp.geturl()).path.rsplit("/", 1)[-1]
        final = dest / safe_name(name)
        part = dest / f".{final.name}.part"
        size = 0
        with part.open("wb") as out:
            while chunk := resp.read(1 << 20):
                size += len(chunk)
                if size > config.FETCH_MAX_BYTES:
                    part.unlink(missing_ok=True)
                    raise DocumentError(f"file is larger than {config.FETCH_MAX_BYTES // 1024**2} MB")
                out.write(chunk)
    os.replace(part, final)
    return final
