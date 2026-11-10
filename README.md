# vault-mcp

MCP server and worker for a personal knowledge vault. The vault is one SQLite database: pages, dated facts
about them, a question queue, saved queries and stored files. Agents (Claude, ChatGPT, Hermes) read it with
SQL and change it with SQL; everything else is derived or a convenience.

## Layout of the data

```
db/vault.db                the vault (the truth)
  pages, facts, questions, files, queries, log        what cannot be inferred
  links, chunks (+ embeddings), fts, documents, blobs  derived; views current_facts, problems
vault/                     the Markdown export, a git repo: one file per page, written after every change
  blobs/<sha256[:16]>.<ext>   the stored files, named by content, never renamed (gitignored, backed up)
  .incoming/                  upload tickets and partial uploads (same mount, so filing is a rename)
documents/                 Andrii's own folders, read-only; Agent/inbox/ is a drop folder
cache/                     text/<sha256[:2]>/<sha256>.txt document extracts and the embedding model
```

The export is for people and for git: it gives every change a diff and a history, and `vault-import`
rebuilds the database from it. Nobody edits it by hand; the next export of a page overwrites the file.

## Tools

| Tool | Does |
|---|---|
| `sql(sql, params)` | One read-only SELECT over every table and view. `embed(text)` and `cosine(a, b)` rank by meaning, `fts` by keywords, `link()` signs download URLs |
| `write(statements, message)` | INSERT, UPDATE and DELETE on `pages`, `facts`, `questions`, `queries`: one transaction, one `log` row, one git commit of the export. Constraints and triggers do the validation and the bookkeeping |
| `search(query)` | Keyword and meaning rank fused over pages, facts, attachment descriptions and non-HTML documents; `{results: [{id, title, text, url, fact}]}`. A fact hit carries full dates, confirmation, source and note separately from the clipped preview |
| `fetch(id)` | A page with its facts, `questions`, `doc:<path>` or `path@commit`; ChatGPT's `{id, title, text, url, metadata}` |
| `upload(...)` | A URL or a ChatGPT attachment is pulled and filed; otherwise a signed PUT link plus a ready `curl`. `replaces="files/x"` files a new version of that page |

`sql` runs on a separate `mode=ro` connection with `query_only`, an authorizer (SELECT/WITH and a few
read-only pragmas), a time limit, row paging and clipped cells. `write` runs under an authorizer that allows
row changes on the four tables and nothing else, and refuses a statement that changes more than 20 rows
unless it says `many`. Lint findings and review queues are views and saved queries (`problems`,
`queries`). Resources: `vault://AGENTS` (the AGENTS page), `vault://schema` (the DDL).
The row guard also covers CTE-prefixed writes. `write` rejects SELECT and RETURNING; use `sql` for reads.
File metadata changes through upload or page cascades, not direct UPDATE/DELETE on `files`.

## Design

- **A small canonical model, the rest is inferred.** A page is id, title, body, private, updated. A fact is a claim
  about a page with a validity period (`valid_from`, `valid_until`), the date it was confirmed, a source and
  a note. No kinds, statuses, tags, categories or summaries: the table says what a row is, an empty
  `confirmed` says it is unreviewed, search and links say what it is about.
- **Every change marks what it touched.** Triggers put page ids into `touched`; `drain` re-derives links,
  chunks and embeddings for those pages and re-exports them. The worker drains whatever a crash left behind.
- **Meaning is a column.** Each paragraph and each fact is a chunk with an embedding (a small local model
  run by onnxruntime, by default Microsoft Harrier OSS v1 0.6B; a hashed bag of words when `EMBED_MODEL` is
  empty). The worker embeds new or changed text in the background; search is a query over the column.
- **Blobs are attachments, not knowledge passages.** A file's page describes what it contains and why it
  matters; its body is never extracted, indexed or appended to `fetch`. Use the explicit download/view URL
  when the original is needed. HTML documents are also excluded from extraction and indexing. Ordinary
  non-HTML documents remain content-searchable. No HTML parser, generated previews or blob-content index.

## Evidence retrieval

For complex questions, split the decision into facets and search each independently (in parallel when
possible). Summaries locate evidence; fetch supporting sources, follow `metadata.next_offset`, inspect
dates and uncertainty notes, and seek counterevidence before synthesizing. Read unresolved constraints
from `questions`, not from semantic search. Say "not found" rather than "does not exist"; copied CVs do not
independently corroborate a claim. Stop when important claims have support and remaining gaps are explicit.

The live vault's `AGENTS` page documents this workflow. Saved SQL recipes `semantic-diverse` (at most two
distinct references per page), `evidence` (full fact provenance by page or fact ref), `keyword` and
`open-questions` compose through the existing `sql` tool. They are rows, not new tools or a query planner.
  Default ranking is unchanged. Fact evidence fields are read from `facts`, never inferred from snippets.

Markdown table cells escape pipes and HTML entities, so literal `<br>`, carriage returns and wiki-link
aliases survive export/import. Exports are replaced atomically; malformed table rows abort recovery
instead of silently disappearing. SQLite snapshots remain the lossless backup, including timestamps and log.

## Moving bytes

File bytes never pass through a model or a tool argument:

```
upload(filename) ─▶ curl -T f https://…/u/<ticket>/<name>?e&s ─▶ vault-mcp streams, files ─▶ {"note": "files/x"}
upload(url | ChatGPT file) ─▶ vault-mcp downloads (public addresses only) and files it
Syncthing / Hermes ─▶ documents/Agent/inbox/ ─▶ worker files it
fetch / sql link() ─▶ https://…/f/<blob>/<name>?e&s ─▶ Caddy asks /_verify, then serves from disk
```

Links are HMAC-SHA256 over the path scope and expiry (`signing.py`); `/_verify` is Caddy's `forward_auth`.

## Pages

An HTML file is a page: a person opens it at a stable URL on its own host, `PAGES_URL/<name>/`.
`upload(..., replaces="files/<name>")` files a new version under the same file page, so the URL stays; the
earlier blobs stay in `files.versions` (they count as referenced and cannot be deleted). `fetch` and
`upload` give the URL (`view_url`); in SQL it is `page_url(id)`.

```
browser ─▶ PAGES_URL/<name>/ ─▶ Caddy asks /_page ─▶ X-Blob: <hash>.html ─▶ Caddy serves the blob
browser ─▶ PAGES_URL/        ─▶ Caddy proxies /_pages (the index, newest first)
```

The pages host is meant for the tailnet only (`tailscale serve`), and Caddy sends every page with a CSP
`sandbox` without `allow-same-origin`: each page runs in an opaque origin, with no cookies, storage or
access to other pages or this server. `/_page` and `/_pages` are not routed on the public host.

## Code

```
vault_mcp/
  store.py      the database: schema, constraints, triggers, views; derive/drain; the SQL runners; search
  markdown.py   the export: a page as Markdown and back, `vault-import`
  embed.py      embeddings: the model (or the hashed fallback), cosine, nearest
  repo.py       infra:  git under flock(.git/vault.lock) for the export
  documents.py  infra:  safe paths and names, media kinds, hashing, text extraction cached by hash
  signing.py    infra:  signed download and upload links, verification
  incoming.py   infra:  upload tickets, streamed bodies, fetching public URLs, quota
  filing.py     service: any incoming file -> blob + file page + commit; new versions of a file
  pages.py      service: HTML files as pages: which blob a page URL shows, the index
  worker.py     interface: `vault-worker` (drop folder, publish, documents, push, pruning)
  app/          interface: the MCP server
    server.py     composition root: instructions, auth, HTTP routes (/u PUT, /_verify, /_page, /_pages, /healthz)
    auth.py       GitHub OAuth (allow-listed logins, consent) + named API keys, metrics, error mapping
    tools.py      sql, write, search, fetch, upload and the two resources
    models.py     typed results (output schemas)
```

## Configuration

| Env | Meaning |
|---|---|
| `DB_PATH`, `VAULT_ROOT`, `DOCS_ROOT`, `CACHE_ROOT`, `DROP_REL` | The database, its export (git) and blobs, documents, cache, drop folder |
| `EMBED_MODEL`, `EMBED_FILE`, `EMBED_QUERY_PREFIX`, `EMBED_MAX_TOKENS`, `EMBED_THREADS` | Hugging Face repository and file of the ONNX embedding model (Harrier OSS v1 0.6B), the instruction put before queries, limits; empty model = hashed bag of words |
| `PUBLIC_URL`, `NOTES_BASE_URL`, `LINK_SECRET` | This server's URL (MCP, OAuth, links), SilverBullet for citations, the link key |
| `PAGES_URL` | The pages host, where a person views HTML file notes (empty = no page URLs) |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `JWT_SIGNING_KEY`, `ALLOWED_GITHUB_LOGINS` | OAuth |
| `API_KEYS` | `name=key,…` for headless agents; the name is their identity in commits |
| `FILES_QUOTA_BYTES`, `UPLOAD_MAX_BYTES`, `FETCH_MAX_BYTES`, `LINK_TTL_SECONDS`, `CITATION_TTL_SECONDS`, `SQL_TIMEOUT_SECONDS` | Limits |
| `PUSH_INTERVAL_SECONDS`, `WORKER_INTERVAL_SECONDS`, `DROP_SETTLE_SECONDS` | Worker timing |
| `METRICS_PORT` | Prometheus metrics on a separate port (0 = off) |

## Development

```bash
mise install        # uv, pinned in mise.toml
mise exec -- uvx ruff check src tests
mise exec -- uv run pytest
```
