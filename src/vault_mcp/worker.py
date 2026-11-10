"""The worker: everything that happens without an agent asking.

Each cycle (every WORKER_INTERVAL seconds):
  1. drop folder      files dropped into Agent/inbox (Syncthing, Hermes) -> filing.file()
  2. publish          anything still marked touched is re-derived, exported and committed
  3. derive           the blob listing and missing embeddings every cycle; documents every few cycles
  4. push             the export to its git mirror every PUSH_INTERVAL
  5. prune            expired upload tickets, abandoned partial files, unused text extracts

Every step is idempotent; a crash or restart picks up where it left off.

    vault-worker            # loop forever
    vault-worker --once     # a single pass
"""
from __future__ import annotations

import logging
import sys
import time

from prometheus_client import Counter, Gauge, start_http_server

from . import config, documents, filing, incoming, repo, store

log = logging.getLogger("vault-worker")
SLOW_EVERY = 6  # documents and pruning every SLOW_EVERY cycles (~1 min)

FILED = Counter("vault_worker_files_total", "Files handled by the worker", ["outcome"])
ERRORS = Counter("vault_worker_errors_total", "Failed worker cycles")
DROP_AGE = Gauge("vault_drop_oldest_seconds", "Age of the oldest file waiting in the drop folder")
STORED_BYTES = Gauge("vault_stored_bytes", "Bytes in the blob store")
INDEXED = Gauge("vault_index_items", "Rows in the vault", ["table"])
PROBLEMS = Gauge("vault_problems", "Rows of the problems view", ["kind"])
LAST_RUN = Gauge("vault_worker_last_run_timestamp_seconds", "End of the last successful cycle")
LAST_PUSH = Gauge("vault_last_push_timestamp_seconds", "Last successful push to the git mirror")
_reported: set[str] = set()  # problem kinds seen so far, so a fixed kind drops to 0


def process_drop_folder(now: float) -> float:
    """Files dropped into Agent/inbox on any device; left alone until they stop changing."""
    oldest = 0.0
    drop = config.DOCS_ROOT / config.DROP_REL
    drop.mkdir(parents=True, exist_ok=True)
    for f in list(documents.walk(drop)):
        age = now - f.stat().st_mtime
        oldest = max(oldest, age)
        if age < config.DROP_SETTLE_SECONDS:
            continue
        result = filing.file(f, original_name=documents.safe_name(f.name), origin="manual",
                             added_by="the drop folder")
        FILED.labels("duplicate" if result["duplicate"] else "filed").inc()
        log.info("filed dropped %s as %s", f.name, result["note"])
    return oldest


def push() -> None:
    if not repo.git("remote").split():
        return
    repo.git("push", "-q", "origin", "HEAD")
    LAST_PUSH.set(time.time())


def run_once(cycle: int = 0, last_push: float = 0.0) -> float:
    """One pass; returns the time of the last push."""
    for d in (config.blobs_root(), config.CACHE_ROOT / "text"):
        d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    DROP_AGE.set(process_drop_folder(now))
    with repo.locked(), store.connect() as conn:
        store.check_model(conn)
        store.sync_blobs(conn)
        store.publish(conn, "export: pages changed outside a write")
    with store.connect() as conn:
        store.embed_backlog(conn)
        if cycle % SLOW_EVERY == 0:
            store.sync_documents(conn, extract=True)
            kinds = dict(conn.execute("SELECT kind, count(*) FROM problems GROUP BY kind").fetchall())
            for kind in _reported | set(kinds):
                PROBLEMS.labels(kind).set(kinds.get(kind, 0))
            _reported.update(kinds)
            hashes = {r[0] for r in conn.execute("SELECT sha256 FROM files UNION SELECT sha256 FROM documents")}
            documents.prune_text_cache(hashes)
            incoming.prune(now)
            STORED_BYTES.set(incoming.usage())
        for table in ("pages", "facts", "documents", "chunks"):
            INDEXED.labels(table).set(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
    if config.PUSH_INTERVAL and now - last_push >= config.PUSH_INTERVAL:
        push()
        last_push = now
    LAST_RUN.set(time.time())
    return last_push


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    if "--once" in sys.argv:
        run_once()
        return
    if config.METRICS_PORT:
        start_http_server(config.METRICS_PORT)
    cycle, last_push = 0, 0.0
    while True:
        try:
            last_push = run_once(cycle, last_push)
        except Exception:  # keep looping; the error counter drives the alert
            ERRORS.inc()
            log.exception("worker cycle failed")
        cycle += 1
        time.sleep(config.WORKER_INTERVAL)


if __name__ == "__main__":
    main()
