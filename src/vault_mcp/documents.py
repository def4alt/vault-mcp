"""Andrii's documents under $DOCS_ROOT: safe paths, text extraction cached by content hash, and the
passage index (vec.documents, vec.doc_chunks) that recall searches."""
from __future__ import annotations

import contextlib
import hashlib
import html
import logging
import mimetypes
import os
import re
import sqlite3
import subprocess
import threading
import zipfile
from pathlib import Path

from . import config, embed

log = logging.getLogger("vault-documents")

VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"}
IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".tif", ".tiff"}
DATA = {".csv", ".tsv", ".json", ".jsonl", ".xlsx", ".xls", ".ods", ".parquet", ".yaml", ".yml"}
HTML = {".html", ".htm"}  # attachments to open, not knowledge to extract or index
PLAIN = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml",
         ".tex", ".bib", ".rst", ".org", ".log", ".py", ".js", ".ts", ".go", ".rs", ".java",
         ".kt", ".c", ".h", ".cpp", ".sh", ".sql", ".toml", ".ini", ".srt", ".vtt"}
OFFICE = {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}
EXTRACTABLE = PLAIN | OFFICE | {".pdf"}
MAX_TEXT = 2_000_000
PIECE = 900  # characters a passage aims for
MAX_CHUNKS = 60  # passages indexed per document; the full text stays readable with the document tool
# Slim images ship no /etc/mime.types, so the common types are spelled out.
MIME = {
    ".pdf": "application/pdf", ".md": "text/markdown", ".csv": "text/csv", ".json": "application/json",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".odt": "application/vnd.oasis.opendocument.text", ".odp": "application/vnd.oasis.opendocument.presentation",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet", ".mp4": "video/mp4", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".webm": "video/webm", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".heic": "image/heic",
    ".html": "text/html", ".htm": "text/html", ".txt": "text/plain",
}
_SYNC_LOCK = threading.Lock()


def media_of(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    for media, exts in (("video", VIDEO), ("audio", AUDIO), ("image", IMAGE), ("data", DATA)):
        if ext in exts:
            return media
    return "document"


def mime_of(path: Path | str) -> str:
    return MIME.get(Path(path).suffix.lower()) or mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def resolve(rel: str) -> Path:
    """A documents-relative path as a real path; escapes and hidden paths are refused."""
    root = config.DOCS_ROOT.resolve()
    rel = rel.strip().lstrip("/")
    if not rel:
        raise ValueError("document path must not be empty")
    target = (root / rel).resolve()
    if root not in target.parents:
        raise ValueError(f"`{rel}` is outside the documents tree")
    if any(part.startswith(".") for part in target.relative_to(root).parts):
        raise ValueError(f"`{rel}` is a hidden path")
    return target


def walk(root: Path):
    """Visible files under root, skipping hidden files and folders (.stversions, .stfolder, ...)."""
    for dirpath, dirnames, names in os.walk(root):
        dirnames[:] = sorted(n for n in dirnames if not n.startswith("."))
        for name in sorted(names):
            if not name.startswith("."):
                yield Path(dirpath) / name


def _xml_text(xml: bytes, paragraph: str) -> str:
    text = re.sub(rf"</{paragraph}>", "\n", xml.decode("utf-8", "replace"))
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def _office_text(path: Path) -> str:
    ext = path.suffix.lower()
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if ext == ".docx":
            return _xml_text(z.read("word/document.xml"), "w:p")
        if ext == ".pptx":
            slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                            key=lambda n: int(re.search(r"(\d+)\.xml$", n).group(1)))
            return "\n\n".join(f"## Slide {i}\n{_xml_text(z.read(n), 'a:p')}" for i, n in enumerate(slides, 1))
        if ext == ".xlsx":
            return _xml_text(z.read("xl/sharedStrings.xml"), "si") if "xl/sharedStrings.xml" in names else ""
        return _xml_text(z.read("content.xml"), "text:p")  # OpenDocument


def extract_text(path: Path) -> str | None:
    """Plain text of a file, or None when it has none (media, scans, binaries)."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            out = subprocess.run(["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
                                 capture_output=True, timeout=180, check=True)
            text = out.stdout.decode("utf-8", "replace")
        elif ext in PLAIN:
            with path.open("rb") as fh:
                text = fh.read(MAX_TEXT).decode("utf-8", "replace")
        elif ext in OFFICE:
            text = _office_text(path)
        else:
            return None
    except (OSError, KeyError, zipfile.BadZipFile, subprocess.SubprocessError):
        return None
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_TEXT] or None


def text_for(path: Path, sha: str) -> str | None:
    """Extracted text of a file, cached by content hash under CACHE_ROOT/text (empty file = no text)."""
    if path.suffix.lower() not in EXTRACTABLE:
        return None
    cache = config.CACHE_ROOT / "text" / sha[:2] / f"{sha}.txt"
    if cache.exists():
        return cache.read_text("utf-8", "replace") or None
    text = extract_text(path)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(f".{cache.name}.tmp")
    tmp.write_text(text or "", "utf-8")
    os.replace(tmp, cache)
    return text


def split(text: str, limit: int = MAX_CHUNKS) -> list[str]:
    """Paragraphs merged into passages of about PIECE characters."""
    out: list[str] = []
    current = ""
    for para in filter(None, (p.strip() for p in re.split(r"\n\s*\n", text))):
        if current and len(current) + len(para) > PIECE:
            out.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
        while len(current) > 2 * PIECE:
            out.append(current[: 2 * PIECE])
            current = current[2 * PIECE:]
    if current:
        out.append(current)
    return out[:limit]


def read(rel: str, offset: int = 0, limit: int = 20_000) -> dict[str, object]:
    """Metadata and a page of extracted text for one document."""
    path = resolve(rel)
    if not path.is_file():
        raise FileNotFoundError(f"no document `{rel}`")
    canonical = path.relative_to(config.DOCS_ROOT.resolve()).as_posix()
    st = path.stat()
    out: dict[str, object] = {
        "path": canonical, "name": path.name, "media": media_of(path), "mime": mime_of(path), "size": st.st_size,
    }
    if path.suffix.lower() in HTML:
        out.update(text=None, note="HTML documents are opened with the url, not read as text")
        return out
    text = text_for(path, sha256_of(path))
    if text is None:
        out.update(text=None, note=f"{media_of(path)} without extractable text; open it with the url")
        return out
    start = max(0, offset)
    end = start + max(1, limit)
    out.update(text=text[start:end], offset=start, next_offset=end if end < len(text) else None,
               truncated=end < len(text))
    return out


def _vec_conn(vec_db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(vec_db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def sync(vec_db_path: Path, *, force: bool = False) -> int:
    """Bring vec.documents and vec.doc_chunks in line with the files under DOCS_ROOT.

    A file is re-read when its mtime, size or the embedding model changed; one document is committed at a time,
    so a long first run is resumable and never blocks readers. Returns the number of documents changed or removed.
    """
    root = config.DOCS_ROOT
    if not root.is_dir():
        return 0
    model = embed.model_id()
    with _SYNC_LOCK, contextlib.closing(_vec_conn(vec_db_path)) as vc:
        known = {r["path"]: (r["mtime"], r["size"], r["model"])
                 for r in vc.execute("SELECT path, mtime, size, model FROM documents")}
        seen: set[str] = set()
        changed = 0
        for path in walk(root):
            if path.suffix.lower() in HTML:
                continue
            rel = path.relative_to(root).as_posix()
            seen.add(rel)
            st = path.stat()
            if not force and known.get(rel) == (st.st_mtime, st.st_size, model):
                continue
            sha = sha256_of(path)
            passages = split(text_for(path, sha) or "")
            title = f"{rel.split('/')[0]} / {path.name}" if "/" in rel else path.name
            rows = [(rel, i, t, embed.passage_int8(title, t)) for i, t in enumerate(passages)]
            vc.execute("BEGIN IMMEDIATE")
            try:
                vc.execute("DELETE FROM doc_chunks WHERE path = ?", (rel,))
                vc.execute(
                    "INSERT OR REPLACE INTO documents(path, folder, name, media, mime, size, mtime, sha256, model,"
                    " chunks) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (rel, rel.split("/")[0] if "/" in rel else "", path.name, media_of(path), mime_of(path),
                     st.st_size, st.st_mtime, sha, model, len(rows)),
                )
                vc.executemany("INSERT INTO doc_chunks(path, chunk, text, embedding) VALUES (?, ?, ?, ?)", rows)
                vc.execute("COMMIT")
            except Exception:
                vc.execute("ROLLBACK")
                raise
            changed += 1
        gone = [p for p in known if p not in seen]
        if gone:
            vc.execute("BEGIN IMMEDIATE")
            vc.executemany("DELETE FROM doc_chunks WHERE path = ?", [(p,) for p in gone])
            vc.executemany("DELETE FROM documents WHERE path = ?", [(p,) for p in gone])
            vc.execute("COMMIT")
        if changed or gone:
            log.info("documents index: %d updated, %d removed", changed, len(gone))
        return changed + len(gone)
