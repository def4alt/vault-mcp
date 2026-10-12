import shutil
import subprocess
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

SKELETON = Path(__file__).parent / "fixture"

NOTE = """---
kind: fact
status: unreviewed
as_of: 2026-10-12
categories: [careers]
about: ["[[notes/acme]]"]
summary: I work at Acme.
---
# I work at Acme
"""


@pytest.fixture
def server(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    shutil.copytree(SKELETON, root)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "init", "--allow-empty"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed"], check=True)
    from vault_mcp import server as srv
    monkeypatch.setattr(srv, "ROOT", root)
    return srv.mcp, root


async def call(mcp, tool, **args):
    async with Client(mcp) as c:
        return (await c.call_tool(tool, args)).data


async def test_write_search_backlinks_commit(server):
    mcp, root = server
    out = await call(mcp, "write_note", path="notes/i-work-at-acme", content=NOTE, message="ingest: test")
    assert out.startswith("created")
    assert "ingest: test" in subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
    hits = await call(mcp, "search", query="acme")
    assert hits[0]["path"] == "notes/i-work-at-acme"
    back = await call(mcp, "backlinks", path="notes/acme")
    assert [b["path"] for b in back] == ["notes/i-work-at-acme"]
    rows = await call(mcp, "query", where={"status": "unreviewed", "categories": "careers"})
    assert len(rows) == 1


@pytest.mark.parametrize("path,content,needle", [
    ("notes/x", NOTE.replace("unreviewed", "confirmed"), "only the owner"),
    ("notes/x", NOTE.replace("summary:", "reviewed: 2026-10-12\nsummary:"), "only the owner"),
    ("notes/x", NOTE + "\npassword: hunter2hunter2\n", "credential"),
    ("notes/x", NOTE + "\nIBAN DE89 3704 0044 0532 0130 00\n", "IBAN"),
    ("notes/x", NOTE.replace("careers", "hobbies"), "unknown category"),
    ("AGENTS", NOTE, "may only write"),
    ("notes/../agents", NOTE, "may only write"),
    ("notes/My Note", NOTE, "lowercase"),
])
async def test_write_refusals(server, path, content, needle):
    mcp, _ = server
    with pytest.raises(ToolError, match=needle):
        await call(mcp, "write_note", path=path, content=content, message="x")


async def test_sources_are_immutable(server):
    mcp, _ = server
    src = "---\nkind: source\norigin: manual\nsource_kind: document\nas_of: 2026-10-12\nsummary: s\n---\nraw\n"
    await call(mcp, "write_note", path="sources/raw-manual-2026-10-12", content=src, message="ingest: raw")
    with pytest.raises(ToolError, match="immutable"):
        await call(mcp, "write_note", path="sources/raw-manual-2026-10-12", content=src + "edit", message="x")


async def test_log_and_lint(server):
    mcp, root = server
    await call(mcp, "append_log", op="ingest", target="[[sources/x]]", what="+1 fact", agent="test")
    assert "ingest [[sources/x]]: +1 fact (test)" in (root / "log.md").read_text()
    report = await call(mcp, "lint")
    assert "## Schema violations" in report


async def test_bearer_token_carries_required_scope():
    from vault_mcp.server import BearerVerifier
    tok = await BearerVerifier("s3cret").verify_token("s3cret")
    assert tok is not None and "user" in tok.scopes
    assert await BearerVerifier("s3cret").verify_token("wrong") is None


def test_lint_report_is_valid_and_does_not_hide_orphans(tmp_path):
    import datetime as dt
    from vault_mcp.lint import lint, render
    from vault_mcp.vault import Vault, split_frontmatter
    root = tmp_path / "v"
    shutil.copytree(SKELETON, root)
    (root / "notes/lonely.md").write_text("---\nkind: evergreen\nsummary: s\n---\n# Lonely\n")
    today = dt.date(2026, 10, 12)
    report = render(lint(Vault(root), today), today)
    assert split_frontmatter(report)[0]["kind"] == "reference"
    (root / "notes/lint-2026-10-12.md").write_text(report)
    again = lint(Vault(root), today)
    assert "[[notes/lonely]]" in again["Orphans"]
    assert not again["Schema violations"]
