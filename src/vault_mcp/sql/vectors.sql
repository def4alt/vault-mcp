-- Rebuildable Vector Database (vectors.db, attached as `vec`)
-- Stores scalar-quantized int8 embeddings (<f scale + D int8 = 1,028 B/row at D=1024).
-- Excluded from lean vault.db backups and rebuildable anytime via store.rebuild_vectors().

CREATE TABLE IF NOT EXISTS items_vec (
  item_id      TEXT PRIMARY KEY,
  model        TEXT NOT NULL,
  content_hash BLOB NOT NULL,
  embedding    BLOB NOT NULL
) STRICT;

CREATE TABLE IF NOT EXISTS episodes_vec (
  episode_id   INTEGER PRIMARY KEY,
  model        TEXT NOT NULL,
  content_hash BLOB NOT NULL,
  embedding    BLOB NOT NULL
) STRICT;

-- Documents index: one row per file under $DOCS_ROOT, rebuilt from disk (path is relative to DOCS_ROOT).
CREATE TABLE IF NOT EXISTS documents (
  path   TEXT PRIMARY KEY,
  folder TEXT NOT NULL,
  name   TEXT NOT NULL,
  media  TEXT NOT NULL,
  mime   TEXT NOT NULL,
  size   INTEGER NOT NULL,
  mtime  REAL NOT NULL,
  sha256 TEXT NOT NULL,
  model  TEXT NOT NULL,
  chunks INTEGER NOT NULL DEFAULT 0
) STRICT;

-- Extracted passages of each document with their int8 embeddings.
CREATE TABLE IF NOT EXISTS doc_chunks (
  path      TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  chunk     INTEGER NOT NULL,
  text      TEXT NOT NULL,
  embedding BLOB NOT NULL,
  PRIMARY KEY (path, chunk)
) STRICT;
