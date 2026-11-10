"""Behaviour of the vault server, seen from its clients: tool calls, HTTP, the database, the export and git."""
import functools
import http.server
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

from vault_mcp import config, incoming, markdown, signing, store, worker
from vault_mcp.app.server import build

ACME = ("INSERT INTO pages (id, title, body) VALUES (?, ?, ?)", ["notes/acme", "Acme", "A company in Munich."])
JOB = ("INSERT INTO facts (subject, claim, valid_from, confirmed, source) VALUES (?, ?, ?, ?, ?)",
       ["notes/acme", "I work at Acme.", "2026-01-01", "2026-01-02", "[[sources/chat]]"])


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
    root.mkdir()
    (root / ".gitignore").write_text("blobs/\n.incoming/\n")
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "seed")
    docs = tmp_path / "documents"
    for d in ("Agent/inbox", "University"):
        (docs / d).mkdir(parents=True)
    settings = {"VAULT_ROOT": root, "DOCS_ROOT": docs, "CACHE_ROOT": tmp_path / "cache",
                "DB_PATH": tmp_path / "db/vault.db", "EMBED_MODEL": "", "LINK_SECRET": "s3cret",
                "PUBLIC_URL": "https://vault.example", "NOTES_BASE_URL": "https://notes.example",
                "GITHUB_CLIENT_ID": "", "API_KEYS": {}, "DROP_SETTLE_SECONDS": 0, "PUSH_INTERVAL": 0}
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


async def write(*statements, message="edit: test", many=False, **kw):
    """Statements are SQL strings or (sql, params) pairs."""
    sts = [{"sql": s, "many": many} if isinstance(s, str) else {"sql": s[0], "params": s[1], "many": many}
           for s in statements]
    return await call("write", statements=sts, message=message, **kw)


async def rows(query, *params, **kw):
    return (await call("sql", sql=query, params=list(params), **kw)).rows


def commits(root) -> int:
    return int(git(root, "rev-list", "--count", "HEAD"))


# --- the two primitives ----------------------------------------------------------------

async def test_a_write_is_one_transaction_one_log_row_and_one_commit_of_the_export(env):
    root, _ = env
    before = commits(root)
    done = await write(ACME, JOB, message="ingest: job")
    assert done.changed == ["notes/acme"] and done.rows == 2 and commits(root) == before + 1
    assert await rows("SELECT claim, confirmed FROM current_facts WHERE subject = 'notes/acme'") == [
        {"claim": "I work at Acme.", "confirmed": "2026-01-02"}]
    exported = (root / "notes/acme.md").read_text()
    assert exported.startswith("# Acme\n\nA company in Munich.\n") and "| I work at Acme. | 2026-01-01 |" in exported
    assert "ingest: job" in git(root, "log", "-1", "--format=%s")
    log = await rows("SELECT actor, message, changed, sha FROM log")
    assert log == [{"actor": "mcp", "message": "ingest: job", "changed": '["notes/acme"]', "sha": done.commit}]
    with pytest.raises(ToolError, match="valid_from must be YYYY-MM-DD"):  # the second statement fails: nothing stays
        await write(("INSERT INTO pages (id, title) VALUES ('notes/beta', 'Beta')", None),
                    ("INSERT INTO facts (subject, claim, valid_from) VALUES ('notes/beta', 'x', 'last year')", None))
    assert await rows("SELECT id FROM pages") == [{"id": "notes/acme"}] and commits(root) == before + 1


async def test_write_takes_row_changes_and_nothing_else(env):
    await write(ACME)
    for bad, reason in (("DROP TABLE pages", "not allowed"), ("UPDATE log SET actor = 'me'", "not allowed"),
                        ("INSERT INTO files (id, blob, sha256) VALUES ('notes/acme', 'b', 's')", "not allowed"),
                        ("INSERT INTO facts (subject, claim, valid_from) VALUES ('notes/nope', 'x', '2026-01-01')",
                         "existing page"),
                        ("INSERT INTO pages (id, title) VALUES ('Not A Slug!', 'x')", "page id"),
                        ("UPDATE pages SET body = '| # | Claim | From | Until | Confirmed | Source | Note |' "
                         "WHERE id = 'notes/acme'", "facts table"),
                        ("UPDATE facts SET valid_until = '2020-01-01' WHERE 1", None)):
        if reason:
            with pytest.raises(ToolError, match=reason):
                await write(bad)
    with pytest.raises(ToolError, match="read-only"):
        await call("sql", sql="DELETE FROM pages")
    await write(*[(ACME[0], [f"notes/p{i}", f"P{i}", ""]) for i in range(25)])
    with pytest.raises(ToolError, match="would change 26 rows"):
        await write("UPDATE pages SET private = 1")
    assert (await write("UPDATE pages SET private = 1", many=True)).rows == 26


async def test_a_retried_write_is_applied_once(env):
    root, _ = env
    first = await write(ACME, idempotency_key="k1")
    again = await write(ACME, idempotency_key="k1")
    assert again.replayed and again.commit == first.commit and again.changed == ["notes/acme"]
    assert len(await rows("SELECT id FROM pages")) == 1


async def test_renames_and_deletes_follow_through_and_git_keeps_the_past(env):
    root, _ = env
    first = await write(ACME, JOB, ("INSERT INTO pages (id, title, body) VALUES ('notes/me', 'Me', "
                                    "'I work at [[notes/acme]] and [[notes/acme|there]].')", None))
    await write("UPDATE pages SET id = 'notes/acme-gmbh' WHERE id = 'notes/acme'")
    assert (await rows("SELECT body FROM pages WHERE id = 'notes/me'"))[0]["body"] == \
        "I work at [[notes/acme-gmbh]] and [[notes/acme-gmbh|there]]."
    assert await rows("SELECT subject FROM facts") == [{"subject": "notes/acme-gmbh"}]
    assert await rows("SELECT src FROM links WHERE dst = 'notes/acme-gmbh'") == [{"src": "notes/me"}]
    assert not (root / "notes/acme.md").exists() and "acme-gmbh" in (root / "notes/me.md").read_text()
    stale = (await rows("SELECT updated FROM pages WHERE id = 'notes/me'"))[0]["updated"]
    await write(("UPDATE pages SET body = 'new' WHERE id = 'notes/me' AND updated = ?", [stale]))
    lost = await write(("UPDATE pages SET body = 'mine' WHERE id = 'notes/me' AND updated = ?", [stale]))
    assert lost.rows == 0 and (await rows("SELECT body FROM pages WHERE id = 'notes/me'"))[0]["body"] == "new"
    await write("DELETE FROM pages WHERE id = 'notes/acme-gmbh'")
    assert await rows("SELECT count(*) AS n FROM facts") == [{"n": 0}]
    assert not (root / "notes/acme-gmbh.md").exists()
    assert "I work at Acme." in (await call("fetch", id=f"notes/acme@{first.commit}")).text


# --- the two queries every client needs -------------------------------------------------

async def test_search_ranks_by_keywords_and_meaning_and_a_fact_is_a_hit_of_its_own(env):
    await write(ACME, JOB, ("INSERT INTO pages (id, title, body) VALUES ('notes/alps', 'Alps', "
                            "'Hiking trips in the mountains near Garmisch.')", None))
    top = (await wire("search", query="where do I work"))["results"][0]
    assert set(top) == {"id", "title", "text", "url"} and top["id"] == "notes/acme#fact-1"
    assert "I work at Acme." in top["text"] and "still true" in top["text"] and top["title"] == "Acme"
    assert top["url"] == "https://notes.example/notes/acme"
    assert (await call("search", query="mountains hiking")).results[0].id == "notes/alps"
    page = await call("fetch", id=top["id"])
    assert page.id == "notes/acme" and "| 1 | I work at Acme. | 2026-01-01 |" in page.text
    assert page.metadata["private"] is False and page.metadata["updated"]
    by_meaning = await rows("SELECT ref FROM chunks ORDER BY cosine(embedding, embed(?)) DESC LIMIT 1",
                            "a company in Munich")
    assert by_meaning == [{"ref": "notes/acme"}]
    by_words = await rows("SELECT c.ref FROM fts JOIN chunks c ON c.id = fts.rowid WHERE fts MATCH 'garmisch'")
    assert by_words == [{"ref": "notes/alps"}]
    with pytest.raises(ToolError, match="closest: notes/acme"):
        await call("fetch", id="notes/acme-inc")


async def test_problems_and_the_question_queue_are_queries(env):
    await write(ACME, ("INSERT INTO facts (subject, claim, valid_from, confirmed) VALUES "
                       "('notes/acme', 'Acme has 10 employees.', '2020-01-01', '2020-01-02')", None),
                ("INSERT INTO pages (id, title, body) VALUES ('notes/me', 'Me', 'See [[notes/nowhere]].')", None),
                ("INSERT INTO questions (question, about, asked_by) VALUES ('Is Acme still in Munich?', "
                 "'notes/acme', 'claude')", None),
                ("INSERT INTO queries VALUES ('open', 'SELECT question FROM questions WHERE answer IS NULL', "
                 "'Open questions')", None))
    problems = await rows("SELECT kind, id FROM problems ORDER BY kind, id")
    assert problems == [{"kind": "broken-link", "id": "notes/me"}, {"kind": "orphan", "id": "notes/acme"},
                        {"kind": "question", "id": "questions#1"}, {"kind": "stale", "id": "notes/acme#fact-1"}]
    saved = (await rows("SELECT sql FROM queries WHERE name = 'open'"))[0]["sql"]
    assert await rows(saved) == [{"question": "Is Acme still in Munich?"}]
    assert "Is Acme still in Munich?" in (await call("fetch", id="questions")).text
    await write("UPDATE questions SET answer = 'Yes', answered = date('now') WHERE id = 1",
                "UPDATE facts SET confirmed = date('now') WHERE id = 1")
    assert await rows(saved) == []
    assert [p["kind"] for p in await rows("SELECT kind FROM problems ORDER BY kind")] == ["broken-link", "orphan"]


async def test_the_export_is_a_way_back(env, tmp_path, monkeypatch):
    root, _ = env
    await write(ACME, JOB, "UPDATE pages SET private = 1 WHERE id = 'notes/acme'",
                ("INSERT INTO questions (question, asked_by) VALUES ('Why?', 'me')", None),
                ("INSERT INTO queries VALUES ('all', 'SELECT id FROM pages', 'Every page')", None))
    before = {t: await rows(f"SELECT * FROM {t} ORDER BY 1") for t in ("facts", "questions", "queries")}
    page = await rows("SELECT id, title, body, private FROM pages")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "db/rebuilt.db")
    with store.connect() as conn:
        assert markdown.import_all(conn, root) == 1
    assert {t: await rows(f"SELECT * FROM {t} ORDER BY 1") for t in before} == before
    assert await rows("SELECT id, title, body, private FROM pages") == page


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
    page = (root / "files/report.md").read_text()
    assert "(hermes)" in page and "Q3 numbers" in page and "Tags: finance" in page and "revenue went up" in page
    assert git(root, "ls-files", "blobs") == "" and "files/report.md" in git(root, "ls-files", "files")
    assert (await call("search", query="revenue")).results[0].id == "files/report"
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


PAGE = "<!doctype html><title>Q3 &amp; Q4 report</title><body><h1>Revenue {}</h1></body>"


def shown(base, path, method="GET"):
    """What Caddy's pages host would do for path: (status, blob it serves)."""
    r = httpx2.get(f"{base}/_page", headers={"X-Forwarded-Uri": path, "X-Forwarded-Method": method})
    return r.status_code, r.headers.get("x-blob")


async def test_an_html_file_is_a_page_whose_url_stays_across_new_versions(server, env, tmp_path, monkeypatch):
    root, _ = env
    monkeypatch.setattr(config, "PAGES_URL", "https://pages.example")
    (tmp_path / "report.html").write_text(PAGE.format("went up"))
    async with hermes(server) as c:
        first = (await c.call_tool("upload", {"filename": "report.html", "purpose": "Q3 numbers"})).data
    out = subprocess.run(first.curl, shell=True, cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert '"view_url":"https://pages.example/report/"' in out.replace(" ", "")
    status, v1 = shown(server, "/report/")
    assert status == 200 and (root / "blobs" / v1).read_text() == PAGE.format("went up")

    (tmp_path / "report.html").write_text(PAGE.format("went down"))
    before = commits(root)
    async with hermes(server) as c:
        second = (await c.call_tool("upload", {"filename": "report.html", "replaces": "files/report"})).data
    out = subprocess.run(second.curl, shell=True, cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert '"note":"files/report"' in out.replace(" ", "") and commits(root) == before + 1
    status, v2 = shown(server, "/report")
    assert status == 200 and v2 != v1 and (root / "blobs" / v2).read_text() == PAGE.format("went down")
    page = await call("fetch", id="files/report")
    assert page.url == page.metadata["view_url"] == "https://pages.example/report/"
    assert page.metadata["versions"] == [f"blobs/{v1}"] and "Q3 numbers" in page.text
    assert "went down" in page.text and "went up" not in page.text  # the preview follows the new version
    assert await rows("SELECT page_url(id) AS view FROM files") == [{"view": "https://pages.example/report/"}]

    async with hermes(server) as c:  # the same bytes again change nothing
        again = (await c.call_tool("upload", {"filename": "report.html", "replaces": "files/report"})).data
    assert httpx2.put(again.put_url.replace("{filename}", "report.html"), content=PAGE.format("went down")).json() \
        == {"note": "files/report", "duplicate": True, "view_url": "https://pages.example/report/"}
    with pytest.raises(ToolError, match="belongs to a file page"):  # earlier versions are kept, not orphans
        await write(("DELETE FROM blobs WHERE name = ?", [v1]))
    assert await rows("SELECT * FROM problems WHERE kind = 'blob'") == []

    gallery = httpx2.get(f"{server}/_pages").text
    assert 'href="/report/"' in gallery and "Q3 &amp; Q4 report" in gallery and "Q3 numbers" in gallery


async def test_only_html_files_are_pages(server, env, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PAGES_URL", "https://pages.example")
    (tmp_path / "plan.pdf").write_bytes(b"%PDF-1.4 plan")
    async with hermes(server) as c:
        ticket = (await c.call_tool("upload", {"filename": "plan.pdf"})).data
        with pytest.raises(ToolError, match="not a file page"):
            await c.call_tool("upload", {"filename": "x.html", "replaces": "notes/acme"})
    out = subprocess.run(ticket.curl, shell=True, cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert "view_url" not in out
    assert shown(server, "/plan/") == (404, None) and shown(server, "/missing/") == (404, None)
    assert shown(server, "/../files/plan/")[0] == 404 and shown(server, "/plan/", "POST")[0] == 405


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
    files = await rows("SELECT id, name, mime, link(blob, name) AS url FROM files")
    assert files[0]["mime"] == "application/pdf" and files[0]["url"].startswith("https://vault.example/f/")


async def test_an_unused_stored_file_is_removed_by_deleting_its_row(env):
    root, _ = env
    (root / "blobs").mkdir()
    (root / "blobs/0123456789abcdef.pdf").write_bytes(b"%PDF")
    worker.run_once()
    assert await rows("SELECT id FROM problems WHERE kind = 'blob'") == [{"id": "blobs/0123456789abcdef.pdf"}]
    await write("DELETE FROM blobs WHERE name = '0123456789abcdef.pdf'")
    assert not (root / "blobs/0123456789abcdef.pdf").exists()


# --- worker ----------------------------------------------------------------------------

async def test_the_worker_files_drops_and_publishes_what_is_left_touched(env):
    root, docs = env
    (docs / "Agent/inbox/deck.pptx").write_bytes(b"PK slides")
    with store.connect() as conn:  # a change made behind the tools' back, e.g. by a script
        conn.execute("INSERT INTO pages (id, title, body) VALUES ('notes/typed', 'Typed', 'By hand.')")
    worker.run_once()
    assert not (docs / "Agent/inbox/deck.pptx").exists()
    deck = await rows("SELECT mime FROM files WHERE name = 'deck.pptx'")
    assert deck == [{"mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}]
    assert (root / "notes/typed.md").read_text().startswith("# Typed")
    assert git(root, "log", "-1", "--format=%s", "--", "notes/typed.md")  # exported and committed
    assert (await call("search", query="typed by hand")).results[0].id == "notes/typed"


async def test_the_worker_embeds_text_that_has_no_vector_yet(env):
    await write(ACME)
    with store.connect() as conn:  # as after a model change, or a write while a real model is configured
        conn.execute("UPDATE chunks SET embedding = NULL")
    assert (await call("search", query="company Munich")).results[0].id == "notes/acme"  # keywords still find it
    worker.run_once()
    assert await rows("SELECT count(*) AS n FROM chunks WHERE embedding IS NULL") == [{"n": 0}]


async def test_documents_are_searchable_readable_and_confined(env):
    _, docs = env
    office(docs / "University/lecture.docx",
           {"word/document.xml": "<w:p><w:t>Population coding &amp; noise</w:t></w:p>"})
    worker.run_once()
    hit = (await call("search", query="population coding")).results[0]
    assert hit.id == "doc:University/lecture.docx" and hit.url.startswith("https://vault.example/d/University/")
    assert "Population coding & noise" in (await call("fetch", id=hit.id)).text
    for item in ("doc:../vault/AGENTS.md", "doc:.stversions/x", ".git/config", "notes/x@HEAD:../.git/config"):
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
