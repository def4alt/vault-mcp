"""Ingest worker: inbox -> shared space -> source notes, and documents -> text cache.

A plain polling loop over files. Every step is idempotent, so a crash or restart
picks up where it left off, and the text cache can be deleted and rebuilt.

    vault-worker            # loop forever
    vault-worker --once     # a single pass
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from prometheus_client import Counter, Gauge, start_http_server

from . import config, documents, repo, rules, sharing
from .vault import Vault, render, slugify, unique_path

log = logging.getLogger("vault-worker")
TEXT_EVERY = 6  # refresh the documents text cache every TEXT_EVERY cycles

FILES = Counter("vault_worker_files_total", "Files handled by the worker", ["outcome"])
ERRORS = Counter("vault_worker_errors_total", "Failed worker cycles")
INBOX_AGE = Gauge("vault_inbox_oldest_seconds", "Age of the oldest file waiting in the inbox")
SHARED_BYTES = Gauge("vault_shared_bytes", "Bytes used by the shared space and the inbox")
TEXT_FILES = Gauge("vault_text_cache_files", "Documents with extracted text")
LAST_RUN = Gauge("vault_worker_last_run_timestamp_seconds", "End of the last successful cycle")


def origin_of(client: str) -> str:
    c = client.lower()
    for key, origin in (("claude", "claude"), ("chatgpt", "chatgpt"), ("openai", "chatgpt"), ("hermes", "hermes")):
        if key in c:
            return origin
    return "manual"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


class Hashes:
    """Content hashes of shared files, cached by (size, mtime) under CACHE_ROOT/state."""

    def __init__(self):
        self.path = config.CACHE_ROOT / "state" / "hashes.json"
        self.entries: dict[str, list] = json.loads(self.path.read_text()) if self.path.exists() else {}

    def of(self, rel: str, p: Path) -> str:
        st = p.stat()
        cached = self.entries.get(rel)
        if cached and cached[:2] == [st.st_size, st.st_mtime_ns]:
            return cached[2]
        digest = sha256(p)
        self.entries[rel] = [st.st_size, st.st_mtime_ns, digest]
        return digest

    def find(self, digest: str) -> str | None:
        return next((rel for rel, v in self.entries.items() if v[2] == digest), None)

    def save(self, live: set[str]) -> None:
        self.entries = {k: v for k, v in self.entries.items() if k in live}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries))
        os.replace(tmp, self.path)


def write_source_note(rel: str, meta: dict) -> str:
    src = config.DOCS_ROOT / rel
    text = documents.refresh_cache(rel, src)
    size_mb = src.stat().st_size / 1024**2
    kind = documents.kind_of(src)
    who = meta.get("client") or "Syncthing"
    purpose = (meta.get("purpose") or "").strip()
    preview = (text or "")[:800].strip()
    try:
        rules.check_secrets(preview)
    except rules.RuleViolation:
        preview = ""
    today = dt.date.today().isoformat()
    origin = "web" if meta.get("url") else origin_of(who)
    fm = {"kind": "source", "origin": origin, "source_kind": kind, "as_of": today, "file": rel,
          "url": meta.get("url"),
          "summary": purpose or f"Shared file {src.name} ({size_mb:.1f} MB), not yet distilled."}
    body = (f"# {src.name}\n\nShared file `{rel}` ({kind}, {size_mb:.1f} MB), added by {who} on {today}."
            + (f"\n\nPurpose: {purpose}" if purpose else "")
            + "\n\nRead it with `read_file`, get the original with `download_link`."
            + (f"\n\n## Preview\n\n{preview}" if preview else ""))
    with repo.locked():
        path = unique_path(config.VAULT_ROOT, "sources", f"{slugify(src.stem, 50)}-{origin}-{today}")
        (config.VAULT_ROOT / f"{path}.md").write_text(render(fm, body), "utf-8")
        repo.commit([f"{path}.md"], f"ingest: shared file {src.name} (vault-worker for {who})", "vault-worker")
    log.info("ingested %s as %s", rel, path)
    return path


def _place(f: Path) -> Path:
    month_dir = config.DOCS_ROOT / config.SHARED_REL / dt.date.today().strftime("%Y-%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    name = sharing.safe_name(f.name)
    target, n = month_dir / name, 2
    while target.exists():
        target, n = month_dir / f"{Path(name).stem}-{n}{Path(name).suffix}", n + 1
    os.replace(f, target)
    os.chmod(target, 0o664)
    return target


def process_inbox(hashes: Hashes, noted: dict[str, str]) -> None:
    now, oldest = time.time(), 0.0
    for inbox in sorted(p for p in config.INBOX_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")):
        meta_file = inbox / ".meta.json"
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        results = []
        for f in documents.walk(inbox):
            oldest = max(oldest, now - f.stat().st_mtime)
            if dup := hashes.find(sha256(f)):
                f.unlink()
                results.append({"file": dup, "note": noted.get(dup), "duplicate": True})
                FILES.labels("duplicate").inc()
                continue
            target = _place(f)
            rel = documents.relative(target)
            hashes.of(rel, target)
            noted[rel] = write_source_note(rel, meta)
            results.append({"file": rel, "note": noted[rel]})
            FILES.labels("ingested").inc()
        if results:
            done = config.INBOX_ROOT / ".done" / f"{inbox.name}.json"
            previous = json.loads(done.read_text()) if done.exists() else []
            done.write_text(json.dumps(previous + results))
        if now - meta.get("created", 0) > config.LINK_TTL + 600 and not any(documents.walk(inbox)):
            for p in inbox.iterdir():
                p.unlink()
            inbox.rmdir()
    INBOX_AGE.set(oldest)


def ingest_shared(hashes: Hashes, noted: dict[str, str]) -> set[str]:
    """Give every file in the shared space a source note, however it arrived (Syncthing, Finder...)."""
    live = set()
    for p in documents.walk(config.DOCS_ROOT / config.SHARED_REL):
        rel = documents.relative(p)
        live.add(rel)
        hashes.of(rel, p)
        if rel not in noted:
            noted[rel] = write_source_note(rel, {})
            FILES.labels("ingested").inc()
    return live


def refresh_text_cache() -> None:
    wanted = set()
    for p in documents.walk(config.DOCS_ROOT):
        if p.suffix.lower() not in documents.EXTRACTABLE:
            continue
        rel = documents.relative(p)
        cache = documents.cache_path(rel)
        wanted.add(cache)
        if not cache.exists() or cache.stat().st_mtime < p.stat().st_mtime:
            documents.refresh_cache(rel, p)
    for p in list(documents.walk(config.CACHE_ROOT / "text")):
        if p not in wanted:
            p.unlink()  # derived text whose document is gone
    TEXT_FILES.set(sum(1 for p in wanted if p.stat().st_size))


def run_once(cycle: int = 0) -> None:
    for d in (config.INBOX_ROOT / ".done", config.DOCS_ROOT / config.SHARED_REL, config.CACHE_ROOT / "text"):
        d.mkdir(parents=True, exist_ok=True)
    hashes, noted = Hashes(), Vault(config.VAULT_ROOT).files_noted()
    process_inbox(hashes, noted)
    hashes.save(ingest_shared(hashes, noted))
    if cycle % TEXT_EVERY == 0:
        refresh_text_cache()
    SHARED_BYTES.set(sharing.usage())
    LAST_RUN.set(time.time())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if "--once" in sys.argv:
        run_once()
        return
    if config.METRICS_PORT:
        start_http_server(config.METRICS_PORT)
    cycle = 0
    while True:
        try:
            run_once(cycle)
        except Exception:  # keep looping; the error counter drives the alert
            ERRORS.inc()
            log.exception("worker cycle failed")
        cycle += 1
        time.sleep(config.WORKER_INTERVAL)


if __name__ == "__main__":
    main()
