"""Pages: HTML files, viewable at a stable URL on their own host.

    <PAGES_URL>/<name>/   the current version of files/<name>; Caddy asks /_page which blob to serve
    <PAGES_URL>/          every page, newest first (/_pages)

The bytes stay an ordinary blob. upload(replaces="files/<name>") files a new version under the same
page, so the URL stays and `files.versions` keeps the earlier blobs. Caddy serves pages on the
tailnet only, with a CSP sandbox: each page gets an opaque origin, without cookies or storage.
"""
from __future__ import annotations

import html
import re
import urllib.parse

from . import config
from .signing import BLOB

NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,79}")
TITLE = re.compile(rb"<title[^>]*>(.*?)</title>", re.I | re.S)


def url(note: str) -> str | None:
    """Where a person views files/<name>, or None when pages are off."""
    return f"{config.PAGES_URL}/{note.removeprefix('files/')}/" if config.PAGES_URL and note else None


def _html_files() -> list:
    from . import store
    with store.connect() as conn:
        return conn.execute("SELECT f.id, f.blob, p.body, p.updated FROM files f JOIN pages p USING (id) "
                            "WHERE f.mime = 'text/html' ORDER BY p.updated DESC, f.id").fetchall()


def view_url(note: str) -> str | None:
    """The page URL of a file page, or None if it is not an HTML page or pages are off."""
    return url(note) if any(r["id"] == note for r in _html_files()) else None


def blob_for(request_path: str) -> str | None:
    """The blob file name Caddy serves for /<name>/ (or /<name>), or None."""
    parts = urllib.parse.unquote(urllib.parse.urlsplit(request_path).path).strip("/").split("/")
    if len(parts) != 1 or not NAME.fullmatch(parts[0]):
        return None
    row = next((r for r in _html_files() if r["id"] == f"files/{parts[0]}"), None)
    if row is None:
        return None
    blob = row["blob"].removeprefix("blobs/")
    return blob if BLOB.fullmatch(blob) and (config.blobs_root() / blob).is_file() else None


def _title(blob: str) -> str | None:
    with (config.blobs_root() / blob).open("rb") as f:
        m = TITLE.search(f.read(65536))
    return html.unescape(" ".join(m.group(1).decode("utf-8", "replace").split())) if m else None


def listing() -> list[dict]:
    """Every page: name, title, summary (the first line of its description), date; newest first."""
    out = []
    for r in _html_files():
        name = r["id"].removeprefix("files/")
        if (blob := blob_for(f"/{name}/")) is None:
            continue
        out.append({"name": name, "title": _title(blob) or name, "summary": r["body"].strip().split("\n", 1)[0],
                    "as_of": r["updated"][:10]})
    return out


def gallery() -> str:
    rows = "".join(
        f'<li><a href="/{p["name"]}/">{html.escape(p["title"])}</a><time>{p["as_of"]}</time>'
        f'<p>{html.escape(p["summary"])}</p></li>' for p in listing()) or "<li><p>No pages yet.</p></li>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vault pages</title>
<style>
:root{{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--link:#1d4ed8}}
@media (prefers-color-scheme: dark){{:root{{--bg:#0c0a09;--fg:#e7e5e4;--muted:#a8a29e;--line:#292524;--link:#93c5fd}}}}
body{{margin:0 auto;max-width:44rem;padding:2rem 16px;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,sans-serif}}
h1{{font-size:1.25rem;margin:0 0 1rem}}ul{{list-style:none;margin:0;padding:0}}
li{{display:grid;grid-template-columns:1fr auto;gap:.25rem 1rem;padding:.75rem 0;border-top:1px solid var(--line)}}
a{{color:var(--link);font-weight:600;text-decoration:none;overflow-wrap:anywhere}}
time{{color:var(--muted);font-variant-numeric:tabular-nums}}
p{{grid-column:1/-1;margin:0;color:var(--muted);font-size:.875rem}}
</style></head>
<body><h1>Vault pages</h1><ul>{rows}</ul></body></html>
"""
