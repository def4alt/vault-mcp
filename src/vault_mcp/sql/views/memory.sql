-- Userspace SQL Lens: Universal Memory, Prospective Cues & SHY Sleep Co-Occurrence
-- Pure deterministic SQL views over the 6 kernel tables.

DROP VIEW IF EXISTS v_cooccurrence_window;
DROP VIEW IF EXISTS v_cooccurrence;
DROP VIEW IF EXISTS v_unconsolidated_episodes;
DROP VIEW IF EXISTS v_due_cues;
DROP VIEW IF EXISTS v_active_items;

CREATE VIEW v_active_items AS
SELECT
  i.id,
  i.kind,
  i.title,
  i.body,
  i.props,
  i.updated_at,
  COUNT(e.id) AS episode_count,
  MAX(e.at) AS last_at,
  CASE
    WHEN COUNT(e.id) = 0 THEN 0.0
    ELSE round(ln(SUM(pow(max(0.05, julianday('now') - julianday(e.at)), -0.5))), 4)
  END AS base_level
FROM items i
LEFT JOIN episode_items ei ON ei.item_id = i.id
LEFT JOIN episodes e ON e.id = ei.episode_id
WHERE i.status = 'active'
GROUP BY i.id, i.kind, i.title, i.body, i.props, i.updated_at;

CREATE VIEW v_due_cues AS
SELECT
  c.id,
  c.item_id,
  i.kind AS item_kind,
  i.title AS item_title,
  c.due_at,
  c.action,
  c.created_at
FROM cues c
LEFT JOIN items i ON i.id = c.item_id
WHERE c.status = 'armed'
  AND c.trigger_kind = 'time'
  AND c.due_at <= strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
ORDER BY c.due_at ASC, c.id ASC;

CREATE VIEW v_unconsolidated_episodes AS
SELECT
  e.id,
  e.at,
  e.kind,
  e.source,
  e.context,
  e.detail,
  e.confidence,
  e.uri
FROM episodes e
WHERE e.kind <> 'sleep'
  AND e.id > COALESCE((SELECT CAST(pos AS INTEGER) FROM cursors WHERE name = 'sleep:nrem'), 0)
ORDER BY e.id ASC;

CREATE VIEW v_cooccurrence AS
SELECT
  a.item_id AS item_a,
  b.item_id AS item_b,
  COUNT(*) AS co_count,
  MAX(e.at) AS last_at
FROM episode_items a
JOIN episode_items b
  ON a.episode_id = b.episode_id
 AND a.item_id < b.item_id
JOIN episodes e
  ON e.id = a.episode_id
GROUP BY a.item_id, b.item_id
ORDER BY co_count DESC, last_at DESC;

CREATE VIEW v_cooccurrence_window AS
SELECT
  a.item_id AS item_a,
  b.item_id AS item_b,
  COUNT(*) AS co_count,
  MAX(e.at) AS last_at
FROM episodes e
JOIN episode_items a ON a.episode_id = e.id
JOIN episode_items b
  ON b.episode_id = e.id
 AND a.item_id < b.item_id
WHERE e.id > COALESCE((SELECT CAST(pos AS INTEGER) FROM cursors WHERE name = 'sleep:shy'), 0)
GROUP BY a.item_id, b.item_id
ORDER BY co_count DESC, last_at DESC;
