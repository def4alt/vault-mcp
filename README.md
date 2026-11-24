# vault-mcp

SQLite-first personal memory MCP server exposed over Streamable HTTP with four tools: `sql`, `recall`, `file`, and `document`.

## Storage model

Two SQLite databases (WAL mode, `STRICT` tables):

- **`vault.db`** holds the six canonical tables (`items`, `links`, `cues`, `episodes`, `episode_items`, `cursors`) managed via numbered forward-only migrations (`migrations/001-initial.sql`, tracked by `PRAGMA user_version`), read-only deterministic views (`v_active_items`, `v_due_cues`, `v_unconsolidated_episodes`, `v_cooccurrence`, `v_cooccurrence_window`, `v_concept_state`, `v_review_queue`, `v_study_next`), and an acyclic `requires` DAG trigger on `links`.
- **`vectors.db`** holds rebuildable scalar-quantized `int8` embeddings (`items_vec`, `episodes_vec`) and the documents index (`documents`, `doc_chunks`: extracted passages of every file under `$DOCS_ROOT`, refreshed every `DOCS_SYNC_SECONDS`) with `blake2b` `content_hash` dirty tracking, attached read-only to `sql()` as `vec`. Excluded from canonical database backups and rebuildable anytime with `vault-rebuild-vectors`.

## MCP tools

- `sql(query, params?, limit=100, cursor?)`: run one parameterized SQL statement against `vault.db` (and read `vec.*` with `embed(text)` / `cosine(a, b)` UDFs) under a SQLite authorizer and timeout. Mutating statements delta-sync `vectors.db` automatically.
- `recall(query, budget=1800, limit=6)`: hybrid `int8` vector KNN across `vec.items_vec` and `vec.episodes_vec`, exact slug/title boost, escaped `LIKE` opaque-code fallback, post-ranking 1-hop `requires`/`contradicts`/`part_of`/`relates` context attachment, armed cues, matching document passages, and token-budget packing.
- `file(action, path?, content?, content_base64?, name?, url?, offset=0, limit=20000)`: path-jailed file access inside `$VAULT_ROOT` (`read`, `write`, `delete`) and the ways a file comes in: `upload_link` (signed PUT URL + `curl -T` command), `fetch` (the server downloads a public URL), `put_blob` (small inline base64). All three file a content-addressed blob and return its `path`, `sha256`, `mime`, `filename` and download `url`. Writes to `usr/views/*.sql` reload userspace SQL views live. Reading a blob returns a signed download `url`.
- `document(path, offset=0, limit=20000)`: extracted text of a document under `$DOCS_ROOT` (PDF via `pdftotext`, Office, plain text; cached by content hash in `$CACHE_ROOT/text`) plus a signed download `url`.

## Uploads and downloads

File bytes move only through signed, expiring links (HMAC over `LINK_SECRET`), never through a model. `PUT /u/<ticket>/<name>` streams an upload into `$VAULT_ROOT/.incoming/` and files it under `blobs/` (`UPLOAD_MAX_BYTES`, default 8 GB; `FILES_QUOTA_BYTES`, default 30 GB). `/f/<blob>/<name>` (stored files) and `/d/<path>` (documents) are served by Caddy from disk after a `forward_auth` check against `/_verify`.

## Auth & environment

Supports FastMCP `MultiAuth` with GitHub OAuth (`ALLOWED_GITHUB_LOGINS`) and static bearer `API_KEYS`, guarded by `Gatekeeper` middleware and Prometheus metrics (`METRICS_PORT`).
