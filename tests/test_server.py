"""Behaviour of the vault server, seen from its clients: tool calls, HTTP, files on disk and git."""
import functools
import http.server
import shutil
import socket
import subprocess
import threading
import time
import zipfile
from pathlib import Path

import httpx2
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from vault_mcp import config, incoming, signing, worker
from vault_mcp.app.server import build

SKELETON = Path(__file__).parent / "fixture"

FACT = """---
kind: fact
status: unreviewed
as_of: 2026-10-12
categories: [careers]
tags: [work]
about: ["[[notes/acme]]"]
summary: I work at Acme.
---
# I work at Acme
"""
ENTITY = """---
kind: entity
summary: Acme, a company.
---
# Acme
"""


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def office(path: Path, parts: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))  # no signing or hooks from the host
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "vault"
    shutil.copytree(SKELETON, root)
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    docs = tmp_path / "documents"
    for d in ("Agent/inbox", "University"):
        (docs / d).mkdir(parents=True)
    settings = {"VAULT_ROOT": root, "DOCS_ROOT": docs, "CACHE_ROOT": tmp_path / "cache", "LINK_SECRET": "s3cret",
                "PUBLIC_URL": "https://vault.example", "NOTES_BASE_URL": "https://notes.example",
                "GITHUB_CLIENT_ID": "", "API_KEYS": {}, "DROP_SETTLE_SECONDS": 0, "COMMIT_QUIET_SECONDS": 0,
                "PUSH_INTERVAL": 0}
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    return root, docs


async def call(tool, **args):
    async with Client(build()) as c:
        return (await c.call_tool(tool, args)).data


async def wire(tool, **args) -> dict:
    """The JSON a client receives."""
    async with Client(build()) as c:
        return (await c.call_tool(tool, args)).structured_content


async def write(path, content, message="edit: test", **change):
    return await call("write", changes=[{"path": path, "content": content, **change}], message=message)


async def rows(query, *params, **kw):
    return (await call("sql", sql=query, params=list(params), **kw)).rows


def commits(root) -> int:
    return int(git(root, "rev-list", "--count", "HEAD"))


# --- reading ---------------------------------------------------------------------------

async def test_search_ranks_and_answers_in_the_shape_chatgpt_expects(env):
    await write("notes/acme", ENTITY)
    await write("notes/i-work-at-acme", FACT)
    await write("notes/acme-history", ENTITY.replace("# Acme", "# Acme was founded in 1999").replace(
        "kind: entity", "kind: evergreen"))
    top = (await wire("search", query="work acme"))["results"][0]
    assert set(top) == {"id", "title", "text", "url"} and "Acme" in top["text"]
    assert top["id"] == "notes/i-work-at-acme" and top["url"] == "https://notes.example/notes/i-work-at-acme"
    assert [r.id for r in (await call("search", query='"founded in 1999"')).results] == ["notes/acme-history"]
    either = [r.id for r in (await call("search", query="1999 work")).results]  # no page has both words
    assert set(either) == {"notes/acme-history", "notes/i-work-at-acme"}


async def test_fetch_reads_pages_old_versions_and_suggests_on_a_miss(env):
    root, _ = env
    first = await write("notes/acme", ENTITY)
    await write("notes/acme", ENTITY.replace("a company", "a company in Munich"))
    page = await call("fetch", id="notes/acme")
    assert "Munich" in page.text and page.metadata["rev"] == git(root, "hash-object", "notes/acme.md").strip()
    assert "Munich" not in (await call("fetch", id=f"notes/acme@{first.commit}")).text
    with pytest.raises(ToolError, match="closest: notes/acme"):
        await call("fetch", id="notes/acme-inc")


async def test_sql_answers_questions_about_links_tags_fields_and_history(env):
    await write("notes/acme", ENTITY)
    await write("notes/i-work-at-acme", FACT, "ingest: job")
    assert await rows("SELECT src, field FROM links WHERE dst = ?", "notes/acme") == [
        {"src": "notes/i-work-at-acme", "field": "about"}]
    assert await rows("SELECT id FROM tags WHERE tag = 'work'") == [{"id": "notes/i-work-at-acme"}]
    assert await rows("SELECT id FROM notes WHERE json_extract(fm, '$.categories[0]') = 'careers'") == [
        {"id": "notes/i-work-at-acme"}]
    history = await rows("SELECT f.id FROM commits c JOIN commit_files f USING (sha) WHERE c.message LIKE 'ingest%'")
    assert history == [{"id": "notes/i-work-at-acme"}]
    hits = await rows("SELECT id, snippet(fts, 3, '[', ']', '…', 8) AS s FROM fts WHERE fts MATCH 'work' "
                      "ORDER BY bm25(fts)")
    assert hits[0]["id"] == "notes/i-work-at-acme" and "[work]" in hits[0]["s"]


async def test_sql_is_read_only_single_statement_and_bounded(env, monkeypatch):
    for statement in ("INSERT INTO notes(id) VALUES ('x')", "DELETE FROM notes", "ATTACH 'x.db' AS x",
                      "PRAGMA journal_mode = DELETE", "SELECT 1; SELECT 2"):
        with pytest.raises(ToolError):
            await call("sql", sql=statement)
    monkeypatch.setattr(config, "SQL_TIMEOUT_SECONDS", 0.2)
    with pytest.raises(ToolError, match="longer than"):
        await call("sql", sql="WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT max(i) FROM n")
    for n in range(3):
        await write(f"notes/thing-{n}", ENTITY.replace("# Acme", f"# Thing {n}"))
    page = await call("sql", sql="SELECT id FROM notes ORDER BY id", limit=2)
    assert page.truncated and [r["id"] for r in page.rows] == ["notes/thing-0", "notes/thing-1"]
    rest = await call("sql", sql="SELECT id FROM notes ORDER BY id", limit=2, cursor=page.next_cursor)
    assert not rest.truncated and [r["id"] for r in rest.rows] == ["notes/thing-2"]
    clipped = (await rows("SELECT printf('%.5000c', 'x') AS long"))[0]["long"]
    assert len(clipped) < 2200 and "substr" in clipped


# --- writing ---------------------------------------------------------------------------

async def test_a_batch_is_one_commit_or_nothing(env):
    root, _ = env
    before = commits(root)
    with pytest.raises(ToolError, match="frontmatter does not match"):
        await call("write", message="ingest", changes=[
            {"path": "notes/acme", "content": ENTITY},
            {"path": "notes/bad", "content": ENTITY.replace("kind: entity", "kind: nonsense")}])
    assert not (root / "notes/acme.md").exists() and commits(root) == before
    result = await call("write", message="ingest", changes=[
        {"path": "notes/acme", "content": ENTITY}, {"path": "notes/i-work-at-acme", "content": FACT}])
    assert commits(root) == before + 1 and set(result.revs) == {"notes/acme.md", "notes/i-work-at-acme.md"}


async def test_base_rev_keeps_agents_from_overwriting_each_other(env):
    root, _ = env
    rev = (await write("notes/acme", ENTITY, base_rev="new")).revs["notes/acme.md"]
    (root / "notes/acme.md").write_text(ENTITY.replace("a company", "edited in Obsidian"))
    with pytest.raises(ToolError, match="changed since you read it"):
        await write("notes/acme", ENTITY.replace("a company", "my version"), base_rev=rev)
    with pytest.raises(ToolError, match="changed since you read it"):
        await write("notes/acme", ENTITY, base_rev="new")
    fresh = (await call("fetch", id="notes/acme")).metadata["rev"]
    await write("notes/acme", ENTITY.replace("a company", "merged"), base_rev=fresh)
    assert "merged" in (root / "notes/acme.md").read_text()


async def test_a_retried_write_is_applied_once(env):
    root, _ = env
    args = {"changes": [{"path": "notes/acme", "content": ENTITY}], "message": "add acme", "idempotency_key": "k-1"}
    first = await call("write", **args)
    (root / "notes/acme.md").write_text(ENTITY.replace("a company", "later edit"))
    again = await call("write", **args)
    assert again.replayed and again.commit == first.commit
    assert "later edit" in (root / "notes/acme.md").read_text()


async def test_move_rewrites_links_and_patches_touch_only_frontmatter(env):
    root, _ = env
    await write("notes/acme", ENTITY)
    await write("notes/i-work-at-acme", FACT)
    before = commits(root)
    await call("write", message="rename", changes=[
        {"path": "notes/acme", "move_to": "notes/acme-corp"},
        {"path": "notes/i-work-at-acme", "frontmatter": {"set": {"status": "confirmed", "reviewed": "2026-10-13"}}}])
    fact = (root / "notes/i-work-at-acme.md").read_text()
    assert commits(root) == before + 1 and not (root / "notes/acme.md").exists()
    assert "[[notes/acme-corp]]" in fact and "status: confirmed" in fact and fact.endswith("# I work at Acme\n")


async def test_content_is_never_screened_and_sources_can_be_corrected(env):
    source = ("---\nkind: source\norigin: manual\nsource_kind: snapshot\nas_of: 2026-10-12\nsummary: s\n---\n"
              "# Snapshot\n\nCall +49 151 55435105 or see [[topics]]\n")
    first = await write("sources/snap", source)  # a phone number that passes the card checksum
    assert "+49 151 55435105" in (await call("fetch", id="sources/snap")).text
    fixed = source.replace("[[topics]]", "[[notes/topics]]")
    await write("sources/snap", fixed, base_rev=first.revs["sources/snap.md"])
    assert "[[notes/topics]]" in (await call("fetch", id="sources/snap")).text
    await call("write", message="notes", changes=[{"path": "notes/pw", "content": ENTITY.replace(
        "# Acme", "# Wifi\n\npassword: hunter2hunter2")}])


async def test_delete_removes_pages_and_only_files_nothing_refers_to(env):
    root, _ = env
    first = await write("notes/acme", ENTITY)
    await call("delete", paths=["notes/acme"], message="duplicate")
    assert not (root / "notes/acme.md").exists()
    assert "Acme" in (await call("fetch", id=f"notes/acme@{first.commit}")).text
    (root / "blobs").mkdir(exist_ok=True)
    (root / "blobs/0123456789abcdef.pdf").write_bytes(b"%PDF")
    await call("delete", paths=["blobs/0123456789abcdef.pdf"], message="unused")
    assert not (root / "blobs/0123456789abcdef.pdf").exists()


# --- files -----------------------------------------------------------------------------

@pytest.fixture
def server(env, monkeypatch):
    """The real HTTP app, as Caddy proxies to it."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(config, "PUBLIC_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(config, "API_KEYS", {"hermes": "hermes-key"})
    app = uvicorn.Server(uvicorn.Config(build().http_app(path="/mcp"), port=port, log_level="error"))
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    while not app.started:
        time.sleep(0.05)
    yield config.PUBLIC_URL
    app.should_exit = True
    thread.join()


def hermes(base):
    return Client(StreamableHttpTransport(f"{base}/mcp", headers={"Authorization": "Bearer hermes-key"}))


async def test_an_agent_with_a_shell_uploads_by_running_the_returned_curl(server, env, tmp_path):
    root, _ = env
    async with hermes(server) as c:
        ticket = (await c.call_tool("upload", {"filename": "report.txt", "purpose": "Q3 numbers",
                                               "tags": ["finance"]})).data
    (tmp_path / "report.txt").write_text("revenue went up")
    out = subprocess.run(ticket.curl, shell=True, cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert '"note":"files/report"' in out.replace(" ", "")
    note = (root / "files/report.md").read_text()
    assert "origin: hermes" in note and "Q3 numbers" in note and "- finance" in note
    assert git(root, "ls-files", "blobs") == "" and "files/report.md" in git(root, "ls-files", "files")
    again = httpx2.put(ticket.put_url.replace("{filename}", "copy.txt"), content=b"revenue went up")
    assert again.status_code == 200 and again.json() == {"note": "files/report", "duplicate": True}
    forged = ticket.put_url.replace("{filename}", "x.txt").replace("s=", "s=A")
    assert httpx2.put(forged, content=b"x").status_code == 403


async def test_downloads_are_checked_for_caddy_before_it_serves_them(server, env):
    _, docs = env
    (docs / "University/plan.pdf").write_bytes(b"%PDF")

    def check(url, method="GET"):
        headers = {"X-Forwarded-Uri": url.removeprefix(server), "X-Forwarded-Method": method}
        return httpx2.get(f"{server}/_verify", headers=headers).status_code

    blob = signing.blob_link("blobs/0123456789abcdef.pdf", "Plan v2.pdf")
    assert check(blob) == 200 and check(signing.document_link("University/plan.pdf")) == 200
    assert check(blob.replace("0123456789abcdef", "fedcba9876543210")) == 403
    assert check(signing.blob_link("blobs/0123456789abcdef.pdf", "x", ttl=-10)) == 410
    assert check(blob, "POST") == 405
    assert check(signing.document_link("University/plan.pdf").replace("University", ".git")) == 403
    with pytest.raises(ValueError):
        signing.document_link(".incoming/x")


async def test_urls_and_chatgpt_attachments_are_pulled_by_the_server(env, tmp_path, monkeypatch):
    with pytest.raises(ToolError, match="non-public"):
        await call("upload", url="http://127.0.0.1:1/secret")
    (tmp_path / "www").mkdir()
    (tmp_path / "www/paper.pdf").write_bytes(b"%PDF-1.4 attention")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path / "www"))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(incoming, "_check_public", lambda url: None)  # stand-in for a public host
    try:
        url = f"http://127.0.0.1:{httpd.server_port}/paper.pdf"
        filed = await call("upload", url=url, purpose="the paper")
        assert filed.note == "files/paper" and not filed.duplicate
        attached = await call("upload", file={"download_url": url, "file_id": "f1", "file_name": "Paper.pdf"})
        assert attached.duplicate and attached.note == "files/paper"
    finally:
        httpd.shutdown()
    files = await rows("SELECT id, name, mime, url FROM files")
    assert files[0]["mime"] == "application/pdf" and files[0]["url"].startswith("https://vault.example/f/")


async def test_the_worker_files_drops_commits_outside_edits_and_reports_problems(env):
    root, docs = env
    (docs / "Agent/inbox/deck.pptx").write_bytes(b"PK slides")
    (root / "notes/typed-in-obsidian.md").write_text(ENTITY.replace("# Acme", "# Typed\n\nSee [[notes/nowhere]]"))
    (root / "notes/acme.sync-conflict-20261013-ABC.md").write_text("conflict copy")
    worker.run_once()
    assert not (docs / "Agent/inbox/deck.pptx").exists()
    deck = await rows("SELECT mime FROM files WHERE name = 'deck.pptx'")
    assert deck == [{"mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}]
    assert "autocommit" in git(root, "log", "-1", "--format=%s", "--", "notes/typed-in-obsidian.md")
    assert "sync-conflict" not in git(root, "ls-files")
    problems = await rows("SELECT kind, id FROM problems WHERE kind IN ('broken-link', 'sync-conflict') ORDER BY kind")
    assert problems == [{"kind": "broken-link", "id": "notes/typed-in-obsidian"},
                        {"kind": "sync-conflict", "id": "notes/acme.sync-conflict-20261013-ABC.md"}]
    await call("delete", paths=["notes/acme.sync-conflict-20261013-ABC.md"], message="merged")
    assert not (root / "notes/acme.sync-conflict-20261013-ABC.md").exists()


async def test_documents_are_searchable_readable_and_confined(env):
    _, docs = env
    office(docs / "University/lecture.docx",
           {"word/document.xml": "<w:p><w:t>Population coding &amp; noise</w:t></w:p>"})
    worker.run_once()
    hit = (await call("search", query="population coding")).results[0]
    assert hit.id == "doc:University/lecture.docx" and hit.url.startswith("https://vault.example/d/University/")
    assert "Population coding & noise" in (await call("fetch", id=hit.id)).text
    for item in ("doc:../vault/AGENTS.md", "doc:.stversions/x", ".git/config"):
        with pytest.raises(ToolError):
            await call("fetch", id=item)


# --- access ----------------------------------------------------------------------------

INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


def test_only_known_api_keys_get_in(server):
    def post(token=None):
        headers = {"Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx2.post(f"{server}/mcp", json=INIT, headers=headers, timeout=10)

    assert post().status_code == 401 and post("wrong").status_code == 401
    ok = post("hermes-key")
    assert ok.status_code == 200 and '"serverInfo"' in ok.text
