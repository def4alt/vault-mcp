# vault-mcp

MCP server and worker for a personal knowledge vault: Markdown notes in git, files stored flat as
content-addressed blobs with one note each, and a read-only documents tree. Agents (Claude.ai, ChatGPT,
Hermes, …) search and read everything, write broadly, share files with each other, and undo their own
changes; git records every write.

## Layout of the data

```
vault/                     git repo (the truth)
  notes/ sources/ projects/   pages with YAML frontmatter and wikilinks
  files/<slug>.md             one note per stored file: tags, categories, about, related, summary, embed
  blobs/<sha256[:16]>.<ext>   the files themselves, named by content, never renamed (gitignored, backed up)
documents/                 Andrii's own folders, read-only; Agent/inbox/ is a drop folder
cache/                     derived, rebuildable: index.sqlite (FTS5) + text/<sha256>.txt extracts
```

## Code

```
vault_mcp/
  vault.py      domain: pages, frontmatter, links, naming                    (pure)
  lint.py       domain: health checks + report, `vault-lint`                 (pure)
  rules.py      domain: the few hard write rules                             (pure)
  repo.py       infra:  git, serialised with flock(.git/vault.lock)
  documents.py  infra:  safe paths, media kinds, hashing, text extraction cached by hash
  index.py      infra:  derived SQLite index (FTS5 BM25, facets, links), `vault-index [--rebuild]`
  sharing.py    infra:  nginx-verified signed links, upload inbox, fetching public URLs
  filing.py     service: any incoming file -> blob + file note + commit + index
  worker.py     interface: `vault-worker` (inbox, drop folder, index sync, extract pruning)
  app/          interface: the MCP server
    server.py     composition root: auth + middleware + instructions, mounts the groups
    auth.py       GitHub OAuth (cloud clients), bearer token (tailnet), access, metrics, error mapping
    knowledge.py  search, fetch, save, move, delete, log, history, revert, lint
    files.py      get_link, upload_link, request_upload, save_from_url, share_file, upload_status
    review.py     review_queue
    widgets.py    MCP Apps (ui://vault/upload.html, ui://vault/review.html); ui/ holds the HTML + bridge
    guidance.py   prompts (ingest, ask, review, tidy, share), resources (AGENTS, schema, index)
```

## MCP surface

- **16 tools**, each with a title and read-only/destructive/open-world annotations and an output schema.
  `search` and `fetch` follow the shape ChatGPT expects (`results[{id, title, url}]`, `{id, title, text, url,
  metadata}`), with scopes, filters, `response_format`, cursors and actionable "closest:" errors.
- **MCP Apps**: `request_upload` renders a drag-and-drop upload widget, `review_queue` a confirm/retire widget.
  Both return the same information as text for hosts without Apps.
- **Prompts** for the workflows, **resources** for conventions and schema. Protocol versions up to
  2026-07-28 are negotiated by the SDK.

Files never pass through a model. Agents with a shell (Claude.ai's sandbox, a terminal) move bytes themselves:
`upload_link` returns a signed PUT URL template and a ready `curl` command, `get_link` a `curl -o` command.
Callers on the tailnet host get links on `FILES_TAILNET_BASE_URL` (no Cloudflare upload cap).

```
request_upload ─▶ signed /u/<id>/ (widget or page) ─PUT─▶ nginx ─▶ inbox/<id>/ ┐
save_from_url / share_file ─▶ server downloads ─▶ inbox/<id>/                  ├─▶ worker ─▶ blobs/ + files/<note>.md
Syncthing / Finder ─▶ documents/Agent/inbox/                                    ┘
get_link ─▶ signed /b/<blob>?n=<name> or /d/<document> ─▶ nginx
```

## Configuration

| Env | Meaning |
|---|---|
| `VAULT_ROOT`, `DOCS_ROOT`, `INBOX_ROOT`, `CACHE_ROOT`, `DROP_REL` | Vault repo, documents, upload inbox, derived cache, drop folder |
| `FILES_BASE_URL`, `NOTES_BASE_URL`, `LINK_SECRET` | File host (nginx), SilverBullet for citations, the `secure_link` secret |
| `FILES_QUOTA_BYTES`, `FETCH_MAX_BYTES`, `LINK_TTL_SECONDS`, `CITATION_TTL_SECONDS` | Limits |
| `BASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `JWT_SIGNING_KEY`, `ALLOWED_GITHUB_LOGINS` | OAuth for cloud clients |
| `BEARER_TOKEN`, `TAILNET_HOSTS` | Static token for headless agents, accepted only on these hosts |
| `METRICS_PORT` | Prometheus metrics on a separate port (0 = off) |

## Development

```bash
uvx ruff check src tests
uv run pytest
```
