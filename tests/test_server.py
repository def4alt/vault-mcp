"""Behaviour of the vault server, seen from its clients: tool calls, HTTP, files on disk and git."""
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
tags: [work]
about: ["[[notes/acme]]"]
summary: I work at Acme.
---
# I work at Acme
"""


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def office(path: Path, parts: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, xml in parts.items():
            z.writestr(name, xml)
    return path


def nginx_signature(expires: str, uri: str, name: str = "") -> str:
    """What nginx computes for secure_link_md5 "$secure_link_expires$uri$arg_n $link_secret"."""
    return base64.urlsafe_b64encode(hashlib.md5(f"{expires}{uri}{name} s3cret".encode()).digest()).decode().rstrip("=")


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    shutil.copytree(SKELETON, root)
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    docs = tmp_path / "documents"
    for d in ("Agent/inbox", "University", ".inbox"):
        (docs / d).mkdir(parents=True)
    settings = {"VAULT_ROOT": root, "DOCS_ROOT": docs, "INBOX_ROOT": docs / ".inbox", "CACHE_ROOT": tmp_path / "cache",
                "LINK_SECRET": "s3cret", "FILES_BASE_URL": "https://files.example",
                "NOTES_BASE_URL": "https://notes.example", "GITHUB_CLIENT_ID": "", "BEARER_TOKEN": "",
                "TAILNET_HOSTS": set(), "DROP_SETTLE_SECONDS": 0}
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    return root, docs


async def call(tool, **args):
    async with Client(build()) as c:
        return (await c.call_tool(tool, args)).data


async def save(path, content, message="edit: test"):
    return await call("save", changes=[{"path": path, "content": content}], message=message)


# --- knowledge -------------------------------------------------------------------

async def test_a_saved_fact_is_found_ranked_linked_and_attributed(env):
    root, _ = env
    await save("notes/i-work-at-acme", NOTE, "ingest: test")
    await save("notes/acme-history", NOTE.replace("I work at Acme", "Acme was founded in 1999")
               .replace("kind: fact", "kind: evergreen").replace("status: unreviewed\n", ""))
    hits = (await call("search", query="work acme")).results
    assert hits[0].id == "notes/i-work-at-acme" and hits[0].url == "https://notes.example/notes/i-work-at-acme"
    linked = (await call("search", filters={"links_to": "notes/acme"})).results
    assert {h.id for h in linked} == {"notes/i-work-at-acme", "notes/acme-history"}
    tagged = (await call("search", scope="notes", filters={"tags": "work", "status": "unreviewed"})).results
    assert [h.id for h in tagged] == ["notes/i-work-at-acme"]
    author, subject = git(root, "log", "-2", "--format=%an|%s").splitlines()[1].split("|")
    assert author.startswith("vault-mcp (") and author != "vault-mcp (local)" and subject.startswith("ingest: test")


async def test_search_pages_with_a_cursor_and_offers_detail(env):
    for i in range(5):
        await save(f"notes/fact-{i}", NOTE.replace("I work at Acme", f"Fact number {i} about acme"))
    first = await call("search", query="acme", limit=2)
    second = await call("search", query="acme", limit=2, cursor=first.next_cursor)
    assert len(first.results) == 2 and first.next_cursor
    assert not {h.id for h in first.results} & {h.id for h in second.results}
    detailed = (await call("search", query="acme", limit=1, response_format="detailed")).results[0]
    assert detailed.status == "unreviewed" and detailed.tags == ["work"] and detailed.score is not None
    assert (await call("search", query="number 3", mode="phrase")).results[0].id == "notes/fact-3"
    assert (await call("search", query=r"number [34]\b", mode="regex")).results


async def test_edits_made_outside_the_server_are_searchable(env):
    root, _ = env
    (root / "notes/typed-in-obsidian.md").write_text(NOTE.replace("I work at Acme", "Zanzibar trip planning"))
    assert (await call("search", query="zanzibar")).results[0].id == "notes/typed-in-obsidian"
    for suffix in ("", "-wal", "-shm"):  # the index is derived: losing it costs nothing
        Path(f"{config.index_path()}{suffix}").unlink(missing_ok=True)
    assert (await call("search", query="zanzibar")).results[0].id == "notes/typed-in-obsidian"


async def test_fetch_returns_text_backlinks_and_suggestions(env):
    await save("notes/acme", "---\nkind: entity\ncategories: [careers]\nsummary: A company.\n---\n# Acme\n")
    await save("notes/i-work-at-acme", NOTE)
    item = await call("fetch", id="notes/acme")
    assert item.title == "Acme" and item.metadata["backlinks"] == ["notes/i-work-at-acme"]
    assert (await call("fetch", id="AGENTS")).text == (env[0] / "AGENTS.md").read_text()
    with pytest.raises(ToolError, match="closest: notes/i-work-at-acme"):
        await call("fetch", id="notes/work-acme")


@pytest.mark.parametrize("path,content,reason", [
    ("notes/x", NOTE + "\npassword: hunter2hunter2\n", "credential"),
    ("notes/x", NOTE + "\nIBAN DE89 3704 0044 0532 0130 00\n", "IBAN"),
    ("notes/x", NOTE.replace("careers", "hobbies"), "unknown category"),
    ("notes/../.git/config", NOTE, "off limits"),
    (".schema/notes", NOTE, "off limits"),
])
async def test_secrets_broken_frontmatter_and_git_internals_are_refused(env, path, content, reason):
    with pytest.raises(ToolError, match=reason):
        await save(path, content)


async def test_agents_may_confirm_facts_and_improve_their_own_rules(env):
    root, _ = env
    await save("notes/x", NOTE)
    await call("save", changes=[{"path": "notes/x", "frontmatter": {"set": {"status": "confirmed",
                                                                            "reviewed": "2026-10-13"}}}],
               message="review: Andrii confirmed")
    await save("AGENTS", "# Rules\n\nImproved by an agent.\n", "edit: rules")
    text = (root / "notes/x.md").read_text()
    assert "status: confirmed" in text and "reviewed: 2026-10-13" in text and "# I work at Acme" in text
    assert "Improved" in (root / "AGENTS.md").read_text()


async def test_sources_stay_as_captured(env):
    src = "---\nkind: source\norigin: manual\nsource_kind: document\nas_of: 2026-10-12\nsummary: s\n---\nraw\n"
    await save("sources/raw-manual-2026-10-12", src)
    with pytest.raises(ToolError, match="raw record"):
        await save("sources/raw-manual-2026-10-12", src + "edit")


async def test_a_save_is_one_commit_and_all_or_nothing(env):
    root, _ = env
    commits = int(git(root, "rev-list", "--count", "HEAD"))
    with pytest.raises(ToolError):
        await call("save", message="batch", changes=[{"path": "notes/good", "content": NOTE},
                                                     {"path": "notes/bad", "content": NOTE + "token: abcdefgh12"}])
    assert not (root / "notes/good.md").exists()
    await call("save", message="batch",
               changes=[{"path": "notes/a", "content": NOTE}, {"path": "notes/b", "content": NOTE}])
    await call("delete", paths=["notes/b"], message="cleanup")
    assert (root / "notes/a.md").exists() and not (root / "notes/b.md").exists()
    assert int(git(root, "rev-list", "--count", "HEAD")) == commits + 2
    assert [h.id for h in (await call("search", query="acme", filters={"folder": "notes"})).results] == ["notes/a"]


async def test_moving_relinks_the_vault_and_revert_undoes_it(env):
    root, _ = env
    await save("notes/acme", "---\nkind: entity\ncategories: [careers]\nsummary: A company.\n---\n# Acme\n")
    await save("notes/fact", NOTE + "\nSee [[notes/acme|Acme]].\n")
    await call("move", path="notes/acme", new_path="notes/acme-corp", message="rename")
    text = (root / "notes/fact.md").read_text()
    assert "[[notes/acme-corp]]" in text and "[[notes/acme-corp|Acme]]" in text
    assert [h.id for h in (await call("search", filters={"links_to": "notes/acme-corp"})).results] == ["notes/fact"]
    latest = (await call("history", limit=1)).commits[0]
    assert latest.message.startswith("rename")
    await call("revert", commit=latest.sha)
    assert (root / "notes/acme.md").exists() and "[[notes/acme|Acme]]" in (root / "notes/fact.md").read_text()


async def test_the_log_names_the_agent_and_lint_reports_health(env):
    root, _ = env
    await call("log", op="ingest", target="[[sources/x]]", what="+1 fact")
    line = (root / "log.md").read_text().strip().splitlines()[-1]
    assert "ingest [[sources/x]]: +1 fact (" in line and "(local)" not in line
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


async def test_reviewing_lists_unreviewed_facts_and_records_decisions(env):
    root, _ = env
    await save("notes/old-fact", NOTE.replace("as_of: 2026-10-12", "as_of: 2025-01-01"))
    await save("notes/new-fact", NOTE)
    async with Client(build()) as c:
        result = await c.call_tool("review_queue", {})
    queue = result.structured_content
    assert [i["id"] for i in queue["items"]] == ["notes/old-fact", "notes/new-fact"]
    assert "2 unreviewed facts" in result.content[0].text
    # what the widget sends when Andrii clicks Confirm
    await call("save", changes=[{"path": "notes/old-fact", "frontmatter": {"set": {"status": "confirmed",
                                                                                     "reviewed": "2026-10-13"}}}],
               message="review: confirmed notes/old-fact (review app)")
    async with Client(build()) as c:
        left = (await c.call_tool("review_queue", {})).structured_content["items"]
    assert [i["id"] for i in left] == ["notes/new-fact"]


# --- files ---------------------------------------------------------------------

async def test_uploads_become_file_notes_with_blobs(env):
    root, _ = env
    async with Client(build()) as c:
        result = await c.call_tool("request_upload", {"purpose": "Robot Vision slides", "tags": ["snu", "slides"]})
    upload = result.structured_content
    assert upload["page_url"] in result.content[0].text  # hosts without MCP Apps still get the link
    # nginx writes the PUT body into the upload's inbox directory
    office(config.INBOX_ROOT / upload["id"] / "Deck 1.pptx",
           {"ppt/slides/slide1.xml": "<a:p><a:t>Depth ablation</a:t></a:p>"})
    worker.run_once()

    status = await call("upload_status", id=upload["id"])
    filed = status.filed[0]
    assert filed.note == "files/deck-1" and filed.blob.startswith("blobs/") and filed.blob.endswith(".pptx")
    assert (root / filed.blob).exists() and not status.waiting
    tracked = git(root, "ls-files").split()
    assert "files/deck-1.md" in tracked and filed.blob not in tracked  # the note is versioned, the bytes are not
    note = await call("fetch", id="files/deck-1")
    assert note.metadata["tags"] == ["slides", "snu"] and note.metadata["summary"] == "Robot Vision slides"
    assert "Depth ablation" in note.text and note.metadata["download_url"].startswith("https://files.example/b/")
    assert (await call("search", query="depth ablation", scope="files")).results[0].id == "files/deck-1"

    again = await call("request_upload", purpose="same deck")
    shutil.copy(root / filed.blob, config.INBOX_ROOT / again.id / "copy.pptx")
    worker.run_once()
    assert (await call("upload_status", id=again.id)).filed[0].duplicate is True
    assert len(list((root / "files").glob("*.md"))) == 1


async def test_an_agent_with_a_shell_uploads_by_itself(env):
    root, _ = env
    link = await call("upload_link", purpose="Generated report", tags=["report"], filename="Q3 report.pdf")
    put = link.put_url_template.replace("{filename}", "Q3%20report.pdf")
    parts = urllib.parse.urlsplit(put)
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert q["s"] == nginx_signature(q["e"], f"/u/{link.id}")  # nginx accepts this PUT
    assert put in link.curl and link.curl.startswith("curl -fsS -T ")
    # what nginx does with the PUT
    target = config.INBOX_ROOT / link.id / urllib.parse.unquote(parts.path.rsplit("/", 1)[1])
    target.write_bytes(b"generated by an agent")
    worker.run_once()
    filed = (await call("upload_status", id=link.id)).filed[0]
    assert filed.note == "files/q3-report" and "tags:\n- report" in (root / "files/q3-report.md").read_text()
    download = await call("get_link", id="files/q3-report")
    assert download.curl == f"curl -fsSL -o 'Q3 report.pdf' '{download.url}'"


async def test_files_dropped_into_the_drop_folder_are_filed(env):
    root, docs = env
    (docs / "Agent/inbox/meeting notes.txt").write_text("Quarterly planning with the robotics team")
    worker.run_once()
    assert not (docs / "Agent/inbox/meeting notes.txt").exists()
    hit = (await call("search", query="robotics planning")).results[0]
    assert hit.id == "files/meeting-notes" and "origin: manual" in (root / "files/meeting-notes.md").read_text()


async def test_download_links_verify_like_nginx(env):
    root, docs = env
    (docs / "University/Lecture 1.pdf").write_bytes(b"%PDF")
    doc = await call("get_link", id="doc:University/Lecture 1.pdf", minutes=5)
    parts = urllib.parse.urlsplit(doc.url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert urllib.parse.unquote(parts.path) == "/d/University/Lecture 1.pdf"
    assert q["s"] == nginx_signature(q["e"], "/d/University/Lecture 1.pdf") and 0 < int(q["e"]) - time.time() <= 300

    (docs / "Agent/inbox/Plan v2.txt").write_text("plan")
    worker.run_once()
    blob = await call("get_link", id="files/plan-v2")
    parts = urllib.parse.urlsplit(blob.url)
    raw_name = parts.query.split("&n=")[1]  # nginx signs $arg_n exactly as sent
    q = dict(urllib.parse.parse_qsl(parts.query))
    assert parts.path.startswith("/b/") and urllib.parse.unquote(raw_name) == "Plan v2.txt"
    assert q["s"] == nginx_signature(q["e"], parts.path, raw_name)


async def test_documents_are_searchable_and_readable(env):
    _, docs = env
    office(docs / "University/lecture.docx",
           {"word/document.xml": "<w:p><w:t>Population coding &amp; noise</w:t></w:p>"})
    worker.run_once()
    hit = (await call("search", query="population coding", scope="documents")).results[0]
    assert hit.id == "doc:University/lecture.docx" and hit.url.startswith("https://files.example/d/")
    assert "Population coding & noise" in (await call("fetch", id=hit.id)).text
    listing = (await call("search", scope="documents", filters={"folder": "University"})).results
    assert [h.id for h in listing] == ["doc:University/lecture.docx"]


async def test_hidden_paths_escapes_and_internal_urls_are_refused(env):
    for item in ("doc:../vault/AGENTS.md", "doc:.inbox/x"):
        with pytest.raises(ToolError):
            await call("fetch", id=item)
    with pytest.raises(ToolError, match="non-public"):
        await call("save_from_url", url="http://127.0.0.1:1/secret", purpose="probe")


async def test_lint_notices_blobs_without_notes(env):
    root, _ = env
    (root / "blobs").mkdir(exist_ok=True)
    (root / "blobs/0123456789abcdef.pdf").write_bytes(b"%PDF")
    assert "blobs/0123456789abcdef.pdf` has no file note" in await call("lint")


async def test_widgets_are_served_as_mcp_apps(env):
    async with Client(build()) as c:
        for uri in ("ui://vault/upload.html", "ui://vault/review.html"):
            content = (await c.read_resource(uri))[0]
            assert content.mime_type == "text/html;profile=mcp-app"
            assert "ui/initialize" in content.text and "/*BRIDGE*/" not in content.text


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
                   "clientInfo": {"name": "mcp", "version": "1"}}}


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


async def test_hermes_gets_tailnet_links_and_attribution(http_server, env, monkeypatch):
    from fastmcp.client.transports import StreamableHttpTransport
    root, _ = env
    monkeypatch.setattr(config, "FILES_TAILNET_BASE_URL", "https://files.tailnet")
    transport = StreamableHttpTransport(http_server, headers={"Host": "vault.tailnet",
                                                              "Authorization": "Bearer hermes-token"})
    async with Client(transport) as c:
        link = (await c.call_tool("upload_link", {"purpose": "from hermes", "filename": "report.txt"})).data
    assert link.put_url_template.startswith("https://files.tailnet/u/hermes-")  # no Cloudflare upload cap
    (config.INBOX_ROOT / link.id / "report.txt").write_text("hermes made this")
    worker.run_once()
    assert "origin: hermes" in (root / "files/report.md").read_text()
