"""Signed, expiring download links: the only way file bytes leave the vault.

    /f/<blob>/<name>   a stored file (blobs/<blob>) saved under `name`; Caddy serves it from blobs/
    /d/<path>          a document under DOCS_ROOT; Caddy serves it from the documents tree

The signature is HMAC-SHA256 over "<scope>\\n<expires>", where the scope is /f/<blob> or /d/<path>
(percent-decoded). Caddy asks /_verify before serving either prefix.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import urllib.parse
from pathlib import Path

from . import config

BLOB = re.compile(r"[0-9a-f]{16,64}(\.[a-z0-9]{1,10})?")


def safe_name(name: str) -> str:
    """A file name that is safe on disk and in a URL path segment."""
    name = Path(urllib.parse.unquote(name or "")).name
    name = re.sub(r"[^\w.\- ()]+", "-", name).strip(" .-")
    return (name or "file")[:120]


def _mac(scope: str, expires: int) -> str:
    if not config.LINK_SECRET:
        raise ValueError("download links are not configured on this server (LINK_SECRET)")
    digest = hmac.new(config.LINK_SECRET.encode(), f"{scope}\n{expires}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:24]).decode()


def scope_of(path: str) -> str | None:
    """The signed part of a decoded link path, or None if it is not a link path."""
    parts = path.split("/")
    if len(parts) == 4 and parts[1] == "f" and BLOB.fullmatch(parts[2]) and parts[3]:
        return f"/f/{parts[2]}"
    if len(parts) >= 3 and parts[1] == "d" and all(p and not p.startswith(".") for p in parts[2:]):
        return path
    return None


def _sign(path: str, ttl: int | None) -> str:
    scope = scope_of(path)
    if scope is None:
        raise ValueError(f"`{path}` cannot be linked")
    expires = int(time.time()) + (config.LINK_TTL if ttl is None else ttl)
    query = urllib.parse.urlencode({"e": expires, "s": _mac(scope, expires)})
    return f"{config.PUBLIC_URL}{urllib.parse.quote(path)}?{query}"


def blob_link(blob: str, name: str | None = None, ttl: int | None = None) -> str:
    """Download link for blobs/<hash>.<ext>, saved as `name`."""
    blob = blob.removeprefix("blobs/")
    return _sign(f"/f/{blob}/{safe_name(name or blob)}", ttl)


def document_link(rel: str, ttl: int | None = None) -> str:
    return _sign("/d/" + rel.strip("/"), ttl)


def verify(raw_uri: str) -> int:
    """HTTP status for a link as requested: 200 valid, 403 forged or malformed, 410 expired."""
    parts = urllib.parse.urlsplit(raw_uri)
    scope = scope_of(urllib.parse.unquote(parts.path))
    query = urllib.parse.parse_qs(parts.query)
    try:
        expires, signature = int(query["e"][0]), query["s"][0]
    except (KeyError, ValueError, IndexError):
        return 403
    if scope is None or not config.LINK_SECRET:
        return 403
    if not hmac.compare_digest(signature.encode(), _mac(scope, expires).encode()):
        return 403
    return 410 if expires < time.time() else 200
