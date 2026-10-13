"""Signed, expiring links: the only credential for moving file bytes.

    /f/<blob>/<name>   a vault file under its original name (Caddy serves it from blobs/)
    /d/<path>          a document (Caddy serves it from the documents tree)
    /u/<id>/<name>     PUT target of an upload ticket (the MCP server streams it into the vault)

The signature is HMAC-SHA256 over "<scope>\\n<expires>". The scope is /f/<blob>, /d/<path> or
/u/<id>, percent-decoded; the name after a blob or ticket is free, so one upload link takes
any number of files and a download can be saved under any name.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time
import urllib.parse

from . import config
from .documents import DocumentError, safe_name

BLOB = re.compile(r"[0-9a-f]{16,64}(\.[a-z0-9]{1,10})?")
TICKET = re.compile(r"[a-z0-9-]{8,64}")


def _mac(scope: str, expires: int) -> str:
    if not config.LINK_SECRET:
        raise DocumentError("file links are not configured on this server")
    digest = hmac.new(config.LINK_SECRET.encode(), f"{scope}\n{expires}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:24]).decode()


def scope_of(path: str) -> str | None:
    """The signed part of a decoded link path, or None if it is not a link path."""
    parts = path.split("/")
    if len(parts) == 4 and parts[1] == "f" and BLOB.fullmatch(parts[2]) and parts[3]:
        return f"/f/{parts[2]}"
    if len(parts) == 4 and parts[1] == "u" and TICKET.fullmatch(parts[2]) and parts[3]:
        return f"/u/{parts[2]}"
    if len(parts) >= 3 and parts[1] == "d" and all(p and not p.startswith(".") for p in parts[2:]):
        return path
    return None


def _sign(path: str, ttl: int | None) -> tuple[str, int]:
    scope = scope_of(path)
    if scope is None:
        raise DocumentError(f"`{path}` cannot be linked")
    expires = int(time.time()) + (ttl or config.LINK_TTL)
    query = urllib.parse.urlencode({"e": expires, "s": _mac(scope, expires)})
    return f"{config.PUBLIC_URL}{urllib.parse.quote(path)}?{query}", expires


def verify(raw_uri: str) -> int:
    """HTTP status for a link as it was requested: 200 valid, 403 forged or malformed, 410 expired."""
    parts = urllib.parse.urlsplit(raw_uri)
    scope = scope_of(urllib.parse.unquote(parts.path))
    query = urllib.parse.parse_qs(parts.query)
    try:
        expires, signature = int(query["e"][0]), query["s"][0]
    except (KeyError, ValueError):
        return 403
    if scope is None or not hmac.compare_digest(signature, _mac(scope, expires)):
        return 403
    return 410 if expires < time.time() else 200


def blob_link(blob: str, name: str, ttl: int | None = None) -> str:
    """Download link for blobs/<hash>.<ext>, saved as `name`."""
    return _sign(f"/f/{blob.removeprefix('blobs/')}/{safe_name(name)}", ttl)[0]


def document_link(rel: str, ttl: int | None = None) -> str:
    return _sign("/d/" + rel.strip("/"), ttl)[0]


def upload_link(ticket: str, ttl: int | None = None) -> tuple[str, int]:
    """(PUT URL template with a {filename} placeholder, expiry)."""
    url, expires = _sign(f"/u/{ticket}/x", ttl)
    path, query = url.split("?", 1)
    return f"{path.rsplit('/', 1)[0]}/{{filename}}?{query}", expires
