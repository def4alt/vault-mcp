-- Userspace SQL Lens: Socratic Tutor & Knowledge Space Theory
-- Pure deterministic SQL views over the 6 kernel tables (compatible with SQLite 3.46+ math fns).

DROP VIEW IF EXISTS v_study_next;
DROP VIEW IF EXISTS v_review_queue;
DROP VIEW IF EXISTS v_concept_state;
DROP VIEW IF EXISTS v_concept_events;

CREATE VIEW v_concept_events AS
SELECT
  i.id AS item_id,
  e.id AS episode_id,
  e.at,
  e.kind,
  e.confidence,
  (julianday(e.at) - julianday(LAG(e.at) OVER (
    PARTITION BY i.id ORDER BY e.at, e.id
  ))) * 24.0 AS gap_hours,
  CASE
    WHEN e.kind IN ('recall', 'transfer')
     AND (julianday(e.at) - julianday(LAG(e.at) OVER (
           PARTITION BY i.id ORDER BY e.at, e.id
         ))) * 24.0 >= 18.0
    THEN 1 ELSE 0
  END AS is_delayed_pass,
  ROW_NUMBER() OVER (PARTITION BY i.id ORDER BY e.at DESC, e.id DESC) AS rn_desc
FROM items i
JOIN episode_items ei ON ei.item_id = i.id
JOIN episodes e ON e.id = ei.episode_id
WHERE i.kind = 'concept'
  AND i.status = 'active'
  AND (ei.role = 'tested' OR (ei.role = 'about' AND e.kind = 'exposure'))
  AND e.kind IN ('exposure', 'assisted', 'partial', 'miss', 'recall', 'transfer');

CREATE VIEW v_concept_state AS
WITH agg AS (
  SELECT
    i.id AS item_id,
    i.title,
    COALESCE(SUM(CASE WHEN ev.kind = 'exposure' THEN 1 ELSE 0 END), 0) AS exposures,
    COALESCE(SUM(CASE WHEN ev.kind IN ('assisted', 'partial', 'miss', 'recall', 'transfer') THEN 1 ELSE 0 END), 0) AS tests,
    COALESCE(SUM(CASE WHEN ev.kind IN ('recall', 'transfer') THEN 1 ELSE 0 END), 0) AS passes,
    COALESCE(SUM(CASE WHEN ev.kind = 'transfer' THEN 1 ELSE 0 END), 0) AS transfers,
    COALESCE(SUM(ev.is_delayed_pass), 0) AS delayed_passes,
    COALESCE(SUM(CASE WHEN ev.kind = 'miss' THEN 1 ELSE 0 END), 0) AS misses,
    MAX(CASE WHEN ev.rn_desc = 1 THEN ev.kind END) AS last_kind,
    MAX(CASE WHEN ev.rn_desc = 1 THEN ev.at END) AS last_at,
    EXISTS (
      SELECT 1
      FROM links l
      JOIN items m ON m.id = l.src
      WHERE l.dst = i.id
        AND l.rel = 'contradicts'
        AND l.status = 'active'
        AND m.kind = 'misconception'
        AND m.status = 'active'
    ) AS has_active_misconception
  FROM items i
  LEFT JOIN v_concept_events ev ON ev.item_id = i.id
  WHERE i.kind = 'concept' AND i.status = 'active'
  GROUP BY i.id, i.title
),
scored AS (
  SELECT
    item_id,
    title,
    exposures,
    tests,
    passes,
    transfers,
    delayed_passes,
    misses,
    last_kind,
    last_at,
    CASE
      WHEN has_active_misconception = 1 OR last_kind = 'miss' THEN 1
      ELSE 0
    END AS contested,
    CASE
      WHEN tests = 0 THEN 0.0
      WHEN last_kind = 'miss' THEN 0.5
      WHEN passes = 0 THEN 0.5
      ELSE round(pow(2.0, min(8, delayed_passes)) * (0.5 + 0.5 * min(passes, 6)), 2)
    END AS stability_days
  FROM agg
),
with_r AS (
  SELECT
    *,
    CASE
      WHEN last_at IS NULL OR stability_days <= 0.0 THEN NULL
      ELSE round(
        pow(2.718281828459045, -(max(0.0, julianday('now') - julianday(last_at))) / stability_days),
        3
      )
    END AS retrievability
  FROM scored
)
SELECT
  item_id,
  title,
  exposures,
  tests,
  passes,
  transfers,
  delayed_passes,
  misses,
  last_kind,
  last_at,
  contested,
  stability_days,
  retrievability,
  CASE
    WHEN contested = 1 THEN 1
    WHEN tests > 0 AND retrievability IS NOT NULL AND retrievability < 0.85 THEN 1
    ELSE 0
  END AS is_due,
  CASE
    WHEN contested = 1 THEN 'contested'
    WHEN tests = 0 AND exposures = 0 THEN 'unseen'
    WHEN tests = 0 AND exposures > 0 THEN 'exposed'
    WHEN passes = 0 THEN 'learning'
    WHEN transfers >= 1 AND delayed_passes >= 2 THEN 'transferred'
    WHEN delayed_passes >= 2 THEN 'robust'
    ELSE 'acquired'
  END AS standing
FROM with_r;

CREATE VIEW v_review_queue AS
SELECT *
FROM v_concept_state
WHERE contested = 1 OR is_due = 1
ORDER BY contested DESC, COALESCE(retrievability, 0.0) ASC, last_at ASC;

CREATE VIEW v_study_next AS
WITH states AS MATERIALIZED (
  SELECT * FROM v_concept_state
)
SELECT cs.*
FROM states cs
WHERE cs.standing IN ('unseen', 'exposed', 'learning')
  AND NOT EXISTS (
    SELECT 1
    FROM links l
    JOIN states prereq ON prereq.item_id = l.src
    WHERE l.dst = cs.item_id
      AND l.rel = 'requires'
      AND l.status = 'active'
      AND prereq.standing IN ('unseen', 'exposed', 'learning', 'contested')
  )
ORDER BY
  CASE cs.standing WHEN 'learning' THEN 0 WHEN 'exposed' THEN 1 ELSE 2 END,
  cs.item_id;
