# vault-mcp

MCP server and worker for a personal knowledge vault: Markdown pages in git, files stored flat as
content-addressed blobs with one note each, a read-only documents tree, and a SQLite index over all of it.
Agents (Claude.ai, ChatGPT, Hermes, collaborators' agents) work on it like on a small database whose rows are
linked notes and whose files are links: six tools, no workflow magic.

## Layout of the data

```
vault/                     git repo (the truth)
  notes/ sources/ projects/   pages with YAML frontmatter and [[wikilinks]]
  files/<slug>.md             one note per stored file: tags, categories, about, related, summary, embed
  blobs/<sha256[:16]>.<ext>   the files themselves, named by content, never renamed (gitignored, backed up)
  .incoming/                  upload tickets and partial uploads (same mount, so filing is a rename)
documents/                 Andrii's own folders, read-only; Agent/inbox/ is a drop folder
cache/                     derived, rebuildable: index.sqlite + text/<sha256>.txt extracts
```

## Tools

| Tool | Does |
|---|---|
| `search(query)` | Ranked full text over pages, file contents and documents; ChatGPT's `{results: [{id, title, text, url}]}` shape |
| `fetch(id)` | A page, `doc:<path>`, a stable uid or `path@commit`; ChatGPT's `{id, title, text, url, metadata}`; `metadata.rev` for writes, `download_url` for files |
| `sql(sql, params)` | Read-only SQLite over `notes`, `documents`, `links`, `tags`, `files`, `fts`, `commits`, `commit_files`, `problems`; `link()` signs download URLs |
| `write(changes, message)` | Create, replace, frontmatter-patch or move pages as one commit; `base_rev` per change, `idempotency_key` per call |
| `delete(paths, message)` | Pages (history keeps them) and blobs no note refers to; the only destructive tool |
| `upload(...)` | A URL or a ChatGPT attachment is pulled and filed; otherwise a signed PUT link plus a ready `curl` |

`sql` runs on a separate `mode=ro` connection with `query_only`, an authorizer (SELECT/WITH and a few
read-only pragmas), a time limit, row paging and clipped cells. Lint findings and review queues are just
queries (`problems`, `notes WHERE status = 'unreviewed'`). Resources: `vault://AGENTS`, `vault://schema`.

## Moving bytes

File bytes never pass through a model or a tool argument:

```
upload(filename) ─▶ curl -T f https://…/u/<ticket>/<name>?e&s ─▶ vault-mcp streams, files ─▶ {"note": "files/x"}
upload(url | ChatGPT file) ─▶ vault-mcp downloads (public addresses only) and files it
Syncthing / Hermes ─▶ documents/Agent/inbox/ ─▶ worker files it
fetch / sql link() ─▶ https://…/f/<blob>/<name>?e&s ─▶ Caddy asks /_verify, then serves from disk
```

Links are HMAC-SHA256 over the path scope and expiry (`signing.py`); `/_verify` is Caddy's `forward_auth`.

## Code

```
vault_mcp/
  vault.py      domain: pages, frontmatter, links, naming                    (pure)
  lint.py       domain: health checks as Problem rows, `vault-lint`          (pure)
  rules.py      domain: the few hard write rules                             (pure)
  repo.py       infra:  git under flock(.git/vault.lock), revs, idempotency keys as commit trailers
  documents.py  infra:  safe paths and names, media kinds, hashing, text extraction cached by hash
  index.py      infra:  the SQLite index (agents' data model) and the read-only SQL runner, `vault-index`
  signing.py    infra:  signed download and upload links, verification
  incoming.py   infra:  upload tickets, streamed bodies, fetching public URLs, quota
  filing.py     service: any incoming file -> blob + file note + commit + index
  worker.py     interface: `vault-worker` (drop folder, autocommit, index, lint, push, pruning)
  app/          interface: the MCP server
    server.py     composition root: instructions, auth, HTTP routes (/u PUT, /_verify, /healthz)
    auth.py       GitHub OAuth (allow-listed logins, consent) + named API keys, metrics, error mapping
    tools.py      the six tools and the two resources
    models.py     typed results (output schemas)
```

## Configuration

| Env | Meaning |
|---|---|
| `VAULT_ROOT`, `DOCS_ROOT`, `CACHE_ROOT`, `DROP_REL` | Vault repo, documents, derived cache, drop folder |
| `PUBLIC_URL`, `NOTES_BASE_URL`, `LINK_SECRET` | This server's URL (MCP, OAuth, links), SilverBullet for citations, the link key |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `JWT_SIGNING_KEY`, `ALLOWED_GITHUB_LOGINS` | OAuth |
| `API_KEYS` | `name=key,…` for headless agents; the name is their identity in commits |
| `FILES_QUOTA_BYTES`, `UPLOAD_MAX_BYTES`, `FETCH_MAX_BYTES`, `LINK_TTL_SECONDS`, `CITATION_TTL_SECONDS`, `SQL_TIMEOUT_SECONDS` | Limits |
| `COMMIT_QUIET_SECONDS`, `PUSH_INTERVAL_SECONDS`, `WORKER_INTERVAL_SECONDS`, `DROP_SETTLE_SECONDS` | Worker timing |
| `METRICS_PORT` | Prometheus metrics on a separate port (0 = off) |

## Development

```bash
uvx ruff check src tests
uv run pytest
```
