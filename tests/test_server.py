"""Behaviour of the vault server, seen from its clients: tool calls, HTTP, files and git."""
import base64
import datetime as dt
import hashlib
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import zipfile
from pathlib import Path

import httpx2
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.exceptions import ToolError

from vault_mcp import config, worker
from vault_mcp.app.server import build

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


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def office(path: Path, parts: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    shutil.copytree(SKELETON, root)
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    docs = tmp_path / "documents"
    for d in ("Agent/shared", "University", ".inbox"):
        (docs / d).mkdir(parents=True)
    settings = {"VAULT_ROOT": root, "DOCS_ROOT": docs, "INBOX_ROOT": docs / ".inbox", "CACHE_ROOT": tmp_path / "cache",
                "LINK_SECRET": "s3cret", "FILES_BASE_URL": "https://files.example", "GITHUB_CLIENT_ID": "",
                "BEARER_TOKEN": "", "TAILNET_HOSTS": set()}
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    return root, docs


async def call(tool, **args):
    async with Client(build()) as c:
        return (await c.call_tool(tool, args)).data


# --- knowledge -------------------------------------------------------------------

async def test_a_written_fact_is_findable_linked_and_attributed(env):
    root, _ = env
    await call("write_note", path="notes/i-work-at-acme", content=NOTE, message="ingest: test")
    assert (await call("search", query="acme"))[0].path == "notes/i-work-at-acme"
    assert [p.path for p in await call("backlinks", path="notes/acme")] == ["notes/i-work-at-acme"]
    assert len(await call("query", where={"status": "unreviewed", "categories": "careers"})) == 1
    author, subject = git(root, "log", "-1", "--format=%an|%s").strip().split("|")
    assert author.startswith("vault-mcp (") and author != "vault-mcp (local)" and subject.startswith("ingest: test")


@pytest.mark.parametrize("path,content,reason", [
    ("notes/x", NOTE + "\npassword: hunter2hunter2\n", "credential"),
    ("notes/x", NOTE + "\nIBAN DE89 3704 0044 0532 0130 00\n", "IBAN"),
    ("notes/x", NOTE.replace("careers", "hobbies"), "unknown category"),
    ("notes/../.git/config", NOTE, "off limits"),
    (".schema/notes", NOTE, "off limits"),
])
async def test_secrets_broken_frontmatter_and_git_internals_are_refused(env, path, content, reason):
    with pytest.raises(ToolError, match=reason):
        await call("write_note", path=path, content=content, message="x")


async def test_agents_may_confirm_facts_and_improve_their_own_rules(env):
    root, _ = env
    confirmed = NOTE.replace("unreviewed", "confirmed").replace("summary:", "reviewed: 2026-10-13\nsummary:")
    await call("write_note", path="notes/x", content=confirmed, message="confirm: Andrii said so")
    await call("write_note", path="AGENTS", content="# Rules\n\nImproved by an agent.\n", message="edit: rules")
    assert "status: confirmed" in (root / "notes/x.md").read_text()
    assert "Improved" in (root / "AGENTS.md").read_text()


async def test_sources_stay_as_captured(env):
    src = "---\nkind: source\norigin: manual\nsource_kind: document\nas_of: 2026-10-12\nsummary: s\n---\nraw\n"
    await call("write_note", path="sources/raw-manual-2026-10-12", content=src, message="ingest: raw")
    with pytest.raises(ToolError, match="raw record"):
        await call("write_note", path="sources/raw-manual-2026-10-12", content=src + "edit", message="x")


async def test_a_batch_is_one_commit_and_all_or_nothing(env):
    root, _ = env
    commits = int(git(root, "rev-list", "--count", "HEAD"))
    with pytest.raises(ToolError):
        await call("apply_changes", message="batch", changes=[
            {"path": "notes/good", "content": NOTE}, {"path": "notes/bad", "content": NOTE + "token: abcdefgh12"}])
    assert not (root / "notes/good.md").exists()
    await call("apply_changes", message="batch", changes=[
        {"path": "notes/a", "content": NOTE}, {"path": "notes/b", "content": NOTE}])
    await call("apply_changes", message="cleanup", changes=[{"path": "notes/b", "delete": True}])
    assert (root / "notes/a.md").exists() and not (root / "notes/b.md").exists()
    assert int(git(root, "rev-list", "--count", "HEAD")) == commits + 2


async def test_moving_a_note_relinks_the_vault_and_revert_undoes_it(env):
    root, _ = env
    entity = "---\nkind: entity\ncategories: [careers]\nsummary: A company.\n---\n# Acme\n"
    await call("write_note", path="notes/acme", content=entity, message="entity")
    await call("write_note", path="notes/fact", content=NOTE + "\nSee [[notes/acme|Acme]].\n", message="fact")
    await call("move_note", path="notes/acme", new_path="notes/acme-corp", message="rename")
    text = (root / "notes/fact.md").read_text()
    assert "[[notes/acme-corp]]" in text and "[[notes/acme-corp|Acme]]" in text
    latest = (await call("history", limit=1))[0]
    assert latest.message.startswith("rename")
    await call("revert", commit=latest.sha)
    assert (root / "notes/acme.md").exists() and "[[notes/acme|Acme]]" in (root / "notes/fact.md").read_text()


async def test_the_log_grows_and_lint_reports_health(env):
    root, _ = env
    await call("append_log", op="ingest", target="[[sources/x]]", what="+1 fact", agent="test")
    assert "ingest [[sources/x]]: +1 fact (test)" in (root / "log.md").read_text()
    assert "## Schema violations (0)" in await call("lint")


def test_saved_lint_reports_are_valid_and_do_not_hide_orphans(env):
    from vault_mcp.lint import lint, render
    from vault_mcp.vault import Vault, split_frontmatter
    root, _ = env
    (root / "notes/lonely.md").write_text("---\nkind: evergreen\nsummary: s\n---\n# Lonely\n")
    today = dt.date(2026, 10, 12)
    report = render(lint(Vault(root), today), today)
    assert split_frontmatter(report)[0]["kind"] == "reference"
    (root / "notes/lint-2026-10-12.md").write_text(report)
    again = lint(Vault(root), today)
    assert "[[notes/lonely]]" in again["Orphans"] and not again["Schema violations"]


# --- documents and sharing -------------------------------------------------------

async def test_office_documents_are_readable_and_searchable(env):
    _, docs = env
    office(docs / "University/lecture.docx",
           {"word/document.xml": "<w:p><w:t>Population coding &amp; noise</w:t></w:p>"})
    office(docs / "University/deck.pptx", {"ppt/slides/slide2.xml": "<a:p><a:t>Second</a:t></a:p>",
                                           "ppt/slides/slide1.xml": "<a:p><a:t>First</a:t></a:p>"})
    worker.run_once()
    hits = await call("search_files", query="population coding")
    assert hits[0].path == "University/lecture.docx" and "Population coding" in hits[0].snippet
    deck = (await call("read_file", path="University/deck.pptx")).text
    assert deck.index("First") < deck.index("Second")
    page = await call("read_file", path="University/lecture.docx", limit=10)
    assert page.text == "Population" and page.next_offset == 10
    listing = {e.path for e in await call("list_files", folder="University")}
    assert listing == {"University/deck.pptx", "University/lecture.docx"}


async def test_documents_outside_the_tree_or_hidden_are_unreachable(env):
    for path in ("../vault/AGENTS.md", ".inbox/x"):
        with pytest.raises(ToolError):
            await call("read_file", path=path)
    with pytest.raises(ToolError, match="non-public"):
        await call("fetch_to_shared", url="http://127.0.0.1:1/secret", purpose="probe")


async def test_download_links_verify_like_nginx_and_name_the_file(env):
    _, docs = env
    (docs / "University/Lecture 1.pdf").write_bytes(b"%PDF")
    link = await call("download_link", path="University/Lecture 1.pdf", minutes=5)
    parts = urllib.parse.urlsplit(link.url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    uri = urllib.parse.unquote(parts.path)  # nginx's $uri is the decoded path
    expected = base64.urlsafe_b64encode(hashlib.md5(f"{q['e']}{uri} s3cret".encode()).digest()).decode().rstrip("=")
    assert uri == "/d/University/Lecture 1.pdf" and q["s"] == expected
    assert 0 < int(q["e"]) - time.time() <= 300


async def test_uploads_become_shared_files_with_source_notes(env):
    root, docs = env
    upload = await call("request_upload", purpose="Robot Vision slides")
    assert upload.url.startswith(f"https://files.example/u/{upload.id}/?")
    # nginx writes the uploaded file into the upload's inbox directory
    office(config.INBOX_ROOT / upload.id / "deck.pptx",
           {"ppt/slides/slide1.xml": "<a:p><a:t>Depth ablation</a:t></a:p>"})
    (docs / "Agent/shared/dropped.txt").write_text("arrived via Syncthing")
    worker.run_once()

    status = await call("upload_status", id=upload.id)
    shared = status.ingested[0]["file"]
    note = (root / f"{status.ingested[0]['note']}.md").read_text()
    assert shared.startswith("Agent/shared/") and not status.waiting
    assert f"file: {shared}" in note and "Robot Vision slides" in note and "Depth ablation" in note
    assert "Depth ablation" in (await call("read_file", path=shared)).text
    assert any("dropped" in p.path for p in await call("search", query="dropped", folder="sources"))

    again = await call("request_upload", purpose="same deck")
    shutil.copy(docs / shared, config.INBOX_ROOT / again.id / "copy.pptx")
    worker.run_once()
    assert (await call("upload_status", id=again.id)).ingested[0]["duplicate"] is True


# --- access ------------------------------------------------------------------------

@pytest.fixture
def http_server(env, monkeypatch):
    monkeypatch.setattr(config, "BEARER_TOKEN", "hermes-token")
    monkeypatch.setattr(config, "TAILNET_HOSTS", {"vault.tailnet"})
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(build().http_app(path="/mcp"), port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join()


INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                   "clientInfo": {"name": "hermes", "version": "1"}}}


def test_hermes_token_works_only_on_the_tailnet_host(http_server):
    def post(host, token=None):
        headers = {"Host": host, "Accept": "application/json, text/event-stream"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return httpx2.post(http_server, json=INIT, headers=headers, timeout=10)

    assert post("vault.tailnet").status_code == 401
    assert post("vault.tailnet", "wrong").status_code == 401
    ok = post("vault.tailnet", "hermes-token")
    assert ok.status_code == 200 and '"serverInfo"' in ok.text
    public = post("mcp.public", "hermes-token")
    assert "only accepted on the tailnet hostname" in public.text and '"serverInfo"' not in public.text
