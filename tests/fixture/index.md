---
kind: index
summary: Entry page of the vault. What lives where and links to the views.
---
# Knowledge vault

Raw material lands in `sources/`, gets distilled into small linked notes in `notes/`, and every operation is recorded in [[log]]. Rules for agents: [[AGENTS]].

## Where things live

- `notes/`: facts, entities, syntheses, evergreens, references. One idea per file.
- `sources/`: raw captures. Never edited.
- `projects/`: one file per project.
- `views/`: saved queries.
- `attachments/`: files.

## Views

| Obsidian | SilverBullet | Shows |
|---|---|---|
| `views/to-review.base` | [[views/to-review]] | Unreviewed facts, oldest first |
| `views/health-check.base` | [[views/health-check]] | Needs-update facts and superseded confirmed facts |
| `views/stale.base` | [[views/stale]] | Confirmed facts not reviewed in 90 days |
| `views/entities.base` | [[views/entities]] | Entities with backlink counts |
| `views/orphans.base` | [[views/orphans]] | Notes with no links in or out |
| `views/to-distill.base` | [[views/to-distill]] | Sources no note links to |
| `views/by-category.base` | [[views/by-category]] | Notes grouped by category |
| `views/syntheses.base` | [[views/syntheses]] | Syntheses and lint reports, newest first |

## Projects

Add links to `projects/` here as projects start and finish.
