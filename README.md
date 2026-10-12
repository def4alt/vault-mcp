# vault-mcp

MCP server for a personal Markdown knowledge vault (see the vault's `AGENTS.md`).

- Streamable HTTP at `/mcp`, health at `/healthz`.
- Claude.ai / ChatGPT: GitHub OAuth through FastMCP's OAuth proxy (DCR, CIMD, PKCE), limited to `ALLOWED_GITHUB_LOGINS`.
- Headless agents: `Authorization: Bearer $BEARER_TOKEN`, accepted only when the request's Host is in `TAILNET_HOSTS`.
- Tools: `search`, `read`, `query`, `backlinks`, `write_note`, `update_frontmatter`, `append_log`, `lint`.
  Writes are checked against `AGENTS.md` rules and `.schema/notes.yaml`, then committed to the vault's git repo.

| Env | Meaning |
|---|---|
| `VAULT_ROOT` | Vault checkout (default `/vault`) |
| `BASE_URL` | Public URL, e.g. `https://mcp.def4alt.com` |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | GitHub OAuth app (optional; without it only the bearer works) |
| `JWT_SIGNING_KEY` | Signs FastMCP tokens and encrypts the OAuth client store in `FASTMCP_HOME` |
| `ALLOWED_GITHUB_LOGINS` | Comma-separated GitHub logins |
| `BEARER_TOKEN`, `TAILNET_HOSTS` | Static token and the hostnames where it is accepted |

`vault-lint <vault> [--write]` runs the same lint from the command line.

```bash
uv run pytest
```
