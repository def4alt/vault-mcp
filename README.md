# vault-mcp

MCP server and ingest worker for a personal Markdown knowledge vault plus a documents tree.
Agents (Claude.ai, ChatGPT, Hermes, …) read everything, write broadly, share files with each other,
and undo their own changes; git records every write.

## Architecture

```
vault_mcp/
  vault.py      domain: pages, frontmatter, links, naming          (pure)
  lint.py       domain: health checks + report, `vault-lint` CLI   (pure)
  rules.py      domain: the few hard write rules                   (pure)
  repo.py       infra:  git, serialised with flock(.git/vault.lock)
  documents.py  infra:  documents tree, text extraction, text cache
  sharing.py    infra:  nginx-verified signed links, inbox, fetching
  config.py     settings from the environment
  worker.py     interface: `vault-worker`, the ingest loop
  app/          interface: the MCP server
    server.py     composition root: auth + middleware, mounts the groups below
    auth.py       GitHub OAuth (cloud clients), bearer token (tailnet), access, metrics, error mapping
    notes.py      search/read/query/backlinks, write/apply_changes/move/frontmatter, history/revert, log, lint
    files.py      list/search/read documents, download links, request_upload, fetch_to_shared, share_file
    guidance.py   prompts (ingest, ask, review, tidy, share) and resources (vault://AGENTS, …)
```

Files never pass through a model:

```
agent ─request_upload─▶ signed /u/<id>/ link ─▶ Andrii's browser ─PUT─▶ nginx ─▶ inbox/<id>/
agent ─fetch_to_shared / share_file─▶ server downloads ─▶ inbox/<id>/
Syncthing / Finder ───────────────────────────────────▶ Agent/shared/
worker: inbox ─▶ Agent/shared/<YYYY-MM>/ (dedup by sha256) ─▶ sources/<note>.md + text cache
agent ─download_link─▶ signed /d/<path> link ─▶ nginx serves the file
```

Hard rules: no secrets in the vault (it is pushed to GitHub), sources are immutable, page frontmatter
matches `.schema/notes.yaml`, `.git` and hidden paths are off limits. Everything else is guidance.

## Configuration

| Env | Meaning |
|---|---|
| `VAULT_ROOT`, `DOCS_ROOT`, `INBOX_ROOT`, `CACHE_ROOT` | Vault repo, documents tree, upload inbox, derived text |
| `FILES_BASE_URL`, `LINK_SECRET` | Public file host and the secret nginx's `secure_link_md5` shares |
| `SHARED_QUOTA_BYTES`, `FETCH_MAX_BYTES`, `LINK_TTL_SECONDS` | Limits |
| `BASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `JWT_SIGNING_KEY`, `ALLOWED_GITHUB_LOGINS` | OAuth for cloud clients |
| `BEARER_TOKEN`, `TAILNET_HOSTS` | Static token for headless agents, accepted only on these hosts |
| `METRICS_PORT` | Prometheus metrics on a separate port (0 = off) |

## Development

```bash
uvx ruff check src tests
uv run pytest
```
