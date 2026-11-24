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
