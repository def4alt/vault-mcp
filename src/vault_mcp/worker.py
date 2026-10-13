"""The worker: files whatever arrives and keeps the index current.

Each cycle:
  1. inbox/<id>/*       (upload page, URL fetches, ChatGPT attachments) -> filing.file()
  2. Agent/inbox/*      (Syncthing drop folder on the Mac and zorya)    -> filing.file()
  3. index sync         (vault pages every cycle; documents with text extraction)
  4. prune              (text extracts no item refers to)

Every step is idempotent; a crash or restart picks up where it left off.

    vault-worker            # loop forever
    vault-worker --once     # a single pass
"""
from __future__ import annotations

import json
import logging
import sys
import time

from prometheus_client import Counter, Gauge, start_http_server

from . import config, documents, filing, index, sharing

log = logging.getLogger("vault-worker")
DOCUMENTS_EVERY = 6  # documents tree scan every DOCUMENTS_EVERY cycles (~1 min)

FILED = Counter("vault_worker_files_total", "Files handled by the worker", ["outcome"])
ERRORS = Counter("vault_worker_errors_total", "Failed worker cycles")
INBOX_AGE = Gauge("vault_inbox_oldest_seconds", "Age of the oldest file waiting to be filed")
STORED_BYTES = Gauge("vault_stored_bytes", "Bytes in the blob store and the inbox")
INDEXED = Gauge("vault_index_items", "Items in the search index", ["scope"])
LAST_RUN = Gauge("vault_worker_last_run_timestamp_seconds", "End of the last successful cycle")


def origin_of(client: str) -> str:
    c = client.lower()
    for key, origin in (("claude", "claude"), ("chatgpt", "chatgpt"), ("openai", "chatgpt"), ("hermes", "hermes")):
        if key in c:
            return origin
    return "manual"


def _record(upload_id: str, results: list[dict]) -> None:
    done = config.INBOX_ROOT / ".done" / f"{upload_id}.json"
    previous = json.loads(done.read_text()) if done.exists() else []
    done.write_text(json.dumps(previous + results))


def process_inbox(now: float) -> float:
    oldest = 0.0
    for box in sorted(p for p in config.INBOX_ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")):
        meta_file = box / ".meta.json"
        meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
        results = []
        for f in documents.walk(box):
            oldest = max(oldest, now - f.stat().st_mtime)
            client = meta.get("client", "an agent")
            result = filing.file(f, original_name=sharing.safe_name(f.name),
                                 origin="web" if meta.get("url") else origin_of(client), added_by=client,
                                 purpose=meta.get("purpose", ""), url=meta.get("url"), tags=meta.get("tags"))
            FILED.labels("duplicate" if result["duplicate"] else "filed").inc()
            results.append(result)
            log.info("filed %s from %s as %s", f.name, box.name, result["note"])
        if results:
            _record(box.name, results)
        if now - meta.get("created", 0) > config.LINK_TTL + 600 and not any(documents.walk(box)):
            for p in box.iterdir():
                p.unlink()
            box.rmdir()
    return oldest


def process_drop_folder(now: float) -> float:
    """Files dropped into Agent/inbox on any Syncthing device; left alone until they stop changing."""
    oldest = 0.0
    drop = config.DOCS_ROOT / config.DROP_REL
    drop.mkdir(parents=True, exist_ok=True)
    for f in list(documents.walk(drop)):
        age = now - f.stat().st_mtime
        if age < config.DROP_SETTLE_SECONDS:
            continue
        oldest = max(oldest, age)
        result = filing.file(f, original_name=sharing.safe_name(f.name), origin="manual", added_by="the drop folder")
        FILED.labels("duplicate" if result["duplicate"] else "filed").inc()
        log.info("filed dropped %s as %s", f.name, result["note"])
    return oldest


def run_once(cycle: int = 0) -> None:
    for d in (config.INBOX_ROOT / ".done", config.blobs_root(), config.CACHE_ROOT / "text"):
        d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    INBOX_AGE.set(max(process_inbox(now), process_drop_folder(now)))
    with index.connect() as conn:
        index.sync_vault(conn)
        if cycle % DOCUMENTS_EVERY == 0:
            index.sync_documents(conn, extract=True)
            documents.prune_text_cache(index.known_hashes(conn))
        for scope, n in conn.execute("SELECT scope, count(*) FROM items GROUP BY scope"):
            INDEXED.labels(scope).set(n)
    STORED_BYTES.set(sharing.usage())
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
