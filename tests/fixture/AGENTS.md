# Vault rules for agents

This vault is my personal knowledge base. Raw material goes into `sources/`. You distill it into small linked notes in `notes/`. Every change you make is logged in `log.md` and committed to git. Follow these rules exactly. If a rule conflicts with a request, say so and ask.

## Layout

| Path | What lives there |
|---|---|
| `index.md` | Entry page. Read it first. |
| `log.md` | Append-only operations log. One line per operation, newest at the bottom. |
| `notes/` | One file per fact, entity, synthesis, evergreen, reference or lint report. Flat: no subfolders. |
| `sources/` | Raw, immutable material: chat snapshots, exports, clippings, documents. |
| `projects/` | One file per project. |
| `views/` | Saved queries: `*.base` for Obsidian, `*.md` for SilverBullet. Don't edit them unless asked. |
| `attachments/` | Images, PDFs, CSVs. |
| `.schema/notes.yaml` | Frontmatter schema. `scripts/lint.py` checks against it. |

## Naming

- Name notes with descriptive titles. Use lowercase words separated by hyphens: `notes/i-prefer-morning-workouts.md`, `notes/dashpoint.md`.
- Name facts in the first person, as a claim: `i-work-at-dashpoint-as-a-contractor`.
- Name entities after the thing itself: `dashpoint`, `tum`, `seoul`.
- Don't put dates in note names. Source names are the exception: `sources/<topic>-<origin>-YYYY-MM-DD.md`.
- Write dates as `YYYY-MM-DD` everywhere.
- Link with wikilinks by path without `.md`: `[[notes/dashpoint]]`, `[[sources/career-claude-2026-10-12]]`.

## Frontmatter

Every note in `notes/` has frontmatter that matches `.schema/notes.yaml`:

```yaml
kind: fact            # fact | entity | synthesis | evergreen | reference | journal | index
status: unreviewed    # facts only: unreviewed | confirmed | needs-update | outdated | historical
as_of: 2026-10-12     # when the claim was true
reviewed:             # only I set this
categories: [careers] # from the fixed list in the schema
source: ["[[sources/...]]"]
about: ["[[notes/<entity>]]"]
related: ["[[notes/...]]"]
supersedes: ["[[notes/...]]"]
projects: ["[[projects/...]]"]
rating:               # 1-7, only for things I actually experienced
summary: One line that states the claim or the point of the note.
```

Sources have `kind: source`, `origin` (claude | chatgpt | hermes | manual | web), `source_kind` (snapshot | chat-export | clipping | document | web-page), `as_of`, `categories`, `summary`, and `url` when there is one.

Projects have `kind: project`, `status` (active | exploring | paused | done | dropped), `area`, `review` (next review date) and `summary` (the next action).

Don't store reverse relations (mentioned in, derived notes, superseded by, linked from). They come from backlinks.

## Ingest

When I give you new material (a chat, a document, a web page, a dump of thoughts):

1. Save the raw material to `sources/` unchanged, with source frontmatter. Never edit it afterwards.
2. Search existing notes and entities for every person, place, project and topic it mentions.
3. Write one fact note per claim. Use a first-person title, `kind: fact`, `status: unreviewed`, `as_of`, `source`, `about` and `related`. Keep the body short: the claim, then context if needed.
4. Reuse existing entities in `about`. Create a new entity note only when at least two facts point at it.
5. When a new fact contradicts an existing one:
   - If the new one is clearly newer, set `supersedes: ["[[notes/old]]"]` on the new note and set the old note's `status: outdated`.
   - If it's unclear which is right, give the new note `status: needs-update` and state the conflict as a question in `summary`.
6. Append one line to `log.md`.
7. Commit.

## Query

When I ask a question:

1. Start from entity notes and their backlinks. Follow links one hop.
2. Answer with wikilinks to the notes you used.
3. Apply the trust rules:
   - `confirmed`: current.
   - `unreviewed`: say it's unconfirmed.
   - `needs-update`, `outdated`, `historical`, superseded notes and anything in `sources/`: past or disputed. Say so.
4. If the answer combines several notes, offer to save it as a `kind: synthesis` note.

## Lint

When I ask for a lint, or when the weekly job runs, report:

- Conflicts: `needs-update` notes, and confirmed notes that another note supersedes.
- Stale facts: `confirmed` with `reviewed` older than 90 days or missing.
- Orphans: no `about`/`related` links out and no backlinks in, excluding `kind: index`.
- Undistilled sources: sources no note links to.
- Missing entities: names that appear in three or more fact titles without an entity note.
- Schema violations against `.schema/notes.yaml`.

Write the report to `notes/lint-YYYY-MM-DD.md` (`kind: reference`), append a log line and commit.

You may fix these without asking:
- add missing links;
- create clearly missing entities;
- mark a note `outdated` when a newer note already supersedes it.

Everything else is a proposal in the report.

## Log

Append one line per operation to `log.md`, never rewrite earlier lines:

```
- 2026-10-12 ingest [[sources/career-claude-2026-10-12]]: +3 facts, +1 entity (claude)
```

Format: `- <date> <op> <target>: <what changed> (<agent>)`, where `op` is one of ingest, query, lint, edit, import.

## Hard rules

- Only I set `status: confirmed` and `reviewed`. Never set them yourself.
- Never edit or delete anything in `sources/`.
- Never store government IDs, account or card numbers, passwords, API keys or other credentials.
- Don't copy long copyrighted passages. Summarize and link instead.
- Don't delete notes. Mark them `outdated` or `historical` and say why.
- Commit every change with a clear message: `<op>: <what> (<agent>)`, for example `ingest: career chat 2026-10-12 (claude)`.

## Through MCP

If you reach the vault through the `vault` MCP server, its tools follow these rules for you:
- they refuse writes to `sources/` (except creating new files there);
- they refuse setting `confirmed` and `reviewed`;
- they commit every write.

Still append the log line yourself with `append_log`.
