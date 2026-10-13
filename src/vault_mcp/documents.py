"""Files on disk: safe paths, media kinds, hashing, and text extraction cached by content hash."""
from __future__ import annotations

import hashlib
import html
import mimetypes
import os
import re
import subprocess
import zipfile
from pathlib import Path

from . import config

VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO = {".mp3", ".m4a", ".wav", ".flac", ".ogg", ".opus", ".aac"}
IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".svg", ".tif", ".tiff"}
DATA = {".csv", ".tsv", ".json", ".jsonl", ".xlsx", ".xls", ".ods", ".parquet", ".yaml", ".yml"}
PLAIN = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".xml", ".html",
         ".htm", ".tex", ".bib", ".rst", ".org", ".log", ".py", ".js", ".ts", ".go", ".rs", ".java",
         ".kt", ".c", ".h", ".cpp", ".sh", ".sql", ".toml", ".ini", ".srt", ".vtt"}
OFFICE = {".docx", ".pptx", ".xlsx", ".odt", ".odp", ".ods"}
EXTRACTABLE = PLAIN | OFFICE | {".pdf"}
MAX_TEXT = 2_000_000


class DocumentError(ValueError):
    pass


def media_of(path: Path | str) -> str:
    ext = Path(path).suffix.lower()
    for media, exts in (("video", VIDEO), ("audio", AUDIO), ("image", IMAGE), ("data", DATA)):
        if ext in exts:
            return media
    return "document"


def mime_of(path: Path | str) -> str:
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


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
    target = (root / rel).resolve()
    if target != root and root not in target.parents:
        raise DocumentError(f"`{rel}` is outside the documents tree")
    if any(part.startswith(".") for part in Path(rel).parts):
        raise DocumentError(f"`{rel}` is a hidden path")
    return target


def relative(path: Path) -> str:
    return str(path.resolve().relative_to(config.DOCS_ROOT.resolve()))


def walk(root: Path):
    """Visible files under root, skipping hidden files and folders (.inbox, .stversions, ...)."""
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


def _cache(sha: str) -> Path:
    return config.CACHE_ROOT / "text" / sha[:2] / f"{sha}.txt"


def cached_text(sha: str) -> str | None:
    p = _cache(sha)
    return (p.read_text("utf-8", "replace") or None) if p.exists() else None


def text_for(path: Path, sha: str) -> str | None:
    """Extracted text of a file, cached by content hash (an empty cache file means "no text")."""
    p = _cache(sha)
    if p.exists():
        return p.read_text("utf-8", "replace") or None
    text = extract_text(path) if path.suffix.lower() in EXTRACTABLE else None
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f".{p.name}.tmp")
    tmp.write_text(text or "", "utf-8")
    os.replace(tmp, p)
    return text


def prune_text_cache(keep: set[str]) -> int:
    """Delete extracts whose content hash no longer appears anywhere."""
    removed = 0
    for p in walk(config.CACHE_ROOT / "text"):
        if p.stem not in keep:
            p.unlink()
            removed += 1
    return removed
