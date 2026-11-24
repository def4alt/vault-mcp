-- Initial Vault schema: 6 STRICT canonical tables and 1 acyclic DAG invariant.
-- Vectors live in rebuildable vectors.db.

CREATE TABLE IF NOT EXISTS items (
  id         TEXT PRIMARY KEY
    CONSTRAINT "item id must be a lowercase slug (a-z, 0-9, -, _)"
    CHECK (length(id) BETWEEN 1 AND 128 AND id GLOB '[a-z0-9]*' AND id NOT GLOB '*[^a-z0-9_-]*'),
  kind       TEXT NOT NULL
    CONSTRAINT "item kind must be one of the 8 core kinds"
    CHECK (kind IN ('concept', 'misconception', 'goal', 'project', 'person', 'source', 'decision', 'theme')),
  title      TEXT NOT NULL
    CONSTRAINT "item title must not be blank"
    CHECK (length(trim(title)) > 0),
  body       TEXT NOT NULL DEFAULT '',
  status     TEXT NOT NULL DEFAULT 'active'
    CONSTRAINT "item status must be active, archived, or rejected"
    CHECK (status IN ('active', 'archived', 'rejected')),
  props      TEXT NOT NULL DEFAULT '{}'
    CONSTRAINT "item props must be a JSON object"
    CHECK (json_valid(props) AND json_type(props) = 'object'),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

CREATE TABLE IF NOT EXISTS links (
  src        TEXT NOT NULL REFERENCES items(id) ON UPDATE CASCADE ON DELETE CASCADE,
  rel        TEXT NOT NULL
    CONSTRAINT "link rel must not be blank"
    CHECK (length(trim(rel)) > 0),
  dst        TEXT NOT NULL REFERENCES items(id) ON UPDATE CASCADE ON DELETE CASCADE,
  weight     REAL NOT NULL DEFAULT 1.0
    CONSTRAINT "link weight must be between 0.0 and 1.0"
    CHECK (weight >= 0.0 AND weight <= 1.0),
  status     TEXT NOT NULL DEFAULT 'active'
    CONSTRAINT "link status must be active or rejected"
    CHECK (status IN ('active', 'rejected')),
  note       TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  PRIMARY KEY (src, rel, dst),
  CONSTRAINT "link cannot point an item to itself" CHECK (src <> dst)
) STRICT;

CREATE TABLE IF NOT EXISTS cues (
  id           INTEGER PRIMARY KEY,
  trigger_kind TEXT NOT NULL
    CONSTRAINT "trigger_kind must be time or event"
    CHECK (trigger_kind IN ('time', 'event')),
  item_id      TEXT REFERENCES items(id) ON UPDATE CASCADE ON DELETE SET NULL,
  on_item_id   TEXT REFERENCES items(id) ON UPDATE CASCADE ON DELETE CASCADE,
  due_at       TEXT,
  action       TEXT NOT NULL
    CONSTRAINT "cue action must not be blank"
    CHECK (length(trim(action)) > 0),
  status       TEXT NOT NULL DEFAULT 'armed'
    CONSTRAINT "cue status must be armed, done, or dismissed"
    CHECK (status IN ('armed', 'done', 'dismissed')),
  created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  CONSTRAINT "time cue requires due_at (without on_item_id); event cue requires on_item_id"
    CHECK (
      (trigger_kind = 'time' AND due_at IS NOT NULL AND on_item_id IS NULL)
      OR (trigger_kind = 'event' AND on_item_id IS NOT NULL)
    )
) STRICT;

CREATE TABLE IF NOT EXISTS episodes (
  id         INTEGER PRIMARY KEY,
  at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  kind       TEXT NOT NULL
    CONSTRAINT "episode kind must be valid"
    CHECK (kind IN ('note', 'decision', 'exposure', 'assisted', 'partial', 'miss', 'recall', 'transfer', 'sleep')),
  source     TEXT NOT NULL DEFAULT 'assistant'
    CONSTRAINT "episode source must not be blank"
    CHECK (length(trim(source)) > 0),
  context    TEXT NOT NULL DEFAULT '',
  detail     TEXT NOT NULL DEFAULT '',
  confidence TEXT
    CONSTRAINT "episode confidence must be NULL, low, or high"
    CHECK (confidence IS NULL OR confidence IN ('low', 'high')),
  uri        TEXT
) STRICT;

CREATE TABLE IF NOT EXISTS episode_items (
  episode_id INTEGER NOT NULL REFERENCES episodes(id) ON UPDATE CASCADE ON DELETE CASCADE,
  item_id    TEXT NOT NULL REFERENCES items(id) ON UPDATE CASCADE ON DELETE CASCADE,
  role       TEXT NOT NULL DEFAULT 'about'
    CONSTRAINT "episode_items role must be valid"
    CHECK (role IN ('about', 'tested', 'misconception', 'project', 'person', 'source')),
  PRIMARY KEY (episode_id, item_id, role)
) STRICT;

CREATE TABLE IF NOT EXISTS cursors (
  name       TEXT PRIMARY KEY
    CONSTRAINT "cursor name must not be blank"
    CHECK (length(trim(name)) > 0),
  pos        TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
) STRICT;

-- Supporting indexes for SQL views and graph traversal
CREATE INDEX IF NOT EXISTS idx_items_kind_status ON items(kind, status);
CREATE INDEX IF NOT EXISTS idx_links_src_rel_status ON links(src, rel, status);
CREATE INDEX IF NOT EXISTS idx_links_dst_rel ON links(dst, rel, status);
CREATE INDEX IF NOT EXISTS idx_cues_status_due ON cues(status, trigger_kind, due_at);
CREATE INDEX IF NOT EXISTS idx_cues_on_item ON cues(on_item_id, status);
CREATE INDEX IF NOT EXISTS idx_episodes_at ON episodes(at, id);
CREATE INDEX IF NOT EXISTS idx_episodes_kind_at ON episodes(kind, at);
CREATE INDEX IF NOT EXISTS idx_ep_items_item ON episode_items(item_id, episode_id);
CREATE INDEX IF NOT EXISTS idx_ep_items_ep_item ON episode_items(episode_id, item_id, role);

-- Sole kernel trigger invariant: active `requires` edges must remain acyclic.
DROP TRIGGER IF EXISTS trg_links_requires_no_cycle_ins;
CREATE TRIGGER trg_links_requires_no_cycle_ins
BEFORE INSERT ON links
WHEN NEW.rel = 'requires' AND NEW.status = 'active'
BEGIN
  SELECT RAISE(ABORT, 'cycle detected in requires DAG')
  WHERE EXISTS (
    WITH RECURSIVE reach(n) AS (
      SELECT NEW.dst
      UNION
      SELECT l.dst
      FROM links l
      JOIN reach r ON l.src = r.n
      WHERE l.rel = 'requires' AND l.status = 'active'
    )
    SELECT 1 FROM reach WHERE n = NEW.src
  );
END;

DROP TRIGGER IF EXISTS trg_links_requires_no_cycle_upd;
CREATE TRIGGER trg_links_requires_no_cycle_upd
BEFORE UPDATE ON links
WHEN NEW.rel = 'requires' AND NEW.status = 'active'
BEGIN
  SELECT RAISE(ABORT, 'cycle detected in requires DAG')
  WHERE EXISTS (
    WITH RECURSIVE reach(n) AS (
      SELECT NEW.dst
      UNION
      SELECT l.dst
      FROM links l
      JOIN reach r ON l.src = r.n
      WHERE l.rel = 'requires'
        AND l.status = 'active'
        AND NOT (l.src = OLD.src AND l.rel = OLD.rel AND l.dst = OLD.dst)
    )
    SELECT 1 FROM reach WHERE n = NEW.src
  );
END;
