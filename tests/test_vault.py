"""Tests for vault-mcp: two-DB split, migrations, int8 vectors, views, path jail, recall, and auth."""
from __future__ import annotations

import contextlib
import datetime as dt
import shutil
import socket
import sqlite3
import threading
import time
from pathlib import Path

import httpx2
import pytest
import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from vault_mcp import config, documents, embed, files, signing, store, uploads
from vault_mcp.app.auth import ApiKeys, Gatekeeper, build_auth
from vault_mcp.app.server import build


def _iso_days_ago(days: float) -> str:
    ts = dt.datetime.now(dt.UTC) - dt.timedelta(days=days)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_item(conn, item_id: str, kind: str = "concept", title: str | None = None, body: str = "") -> None:
    conn.execute(
        "INSERT INTO items(id, kind, title, body) VALUES (?, ?, ?, ?)",
        (item_id, kind, title or item_id.replace("-", " ").title(), body),
    )


def _insert_episode(
    conn,
    kind: str,
    at: str,
    bindings: list[tuple[str, str]],
    *,
    source: str = "assistant",
    context: str = "",
    detail: str = "",
) -> int:
    cur = conn.execute(
        "INSERT INTO episodes(at, kind, source, context, detail) VALUES (?, ?, ?, ?, ?)",
        (at, kind, source, context, detail),
    )
    ep_id = int(cur.lastrowid)
    for item_id, role in bindings:
        conn.execute(
            "INSERT INTO episode_items(episode_id, item_id, role) VALUES (?, ?, ?)",
            (ep_id, item_id, role),
        )
    return ep_id


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "vault"
    root.mkdir()
    db_path = tmp_path / "db" / "vault.db"
    vec_path = tmp_path / "db" / "vectors.db"
    views_dir = root / "usr" / "views"
    views_dir.mkdir(parents=True)
    settings = {
        "VAULT_ROOT": root,
        "DOCS_ROOT": tmp_path / "documents",
        "LINK_SECRET": "test-link-secret",
        "CACHE_ROOT": tmp_path / "cache",
        "DB_PATH": db_path,
        "VEC_DB_PATH": None,
        "VIEWS_DIR": views_dir,
        "EMBED_MODEL": "",
        "PUBLIC_URL": "https://vault.example",
        "GITHUB_CLIENT_ID": "",
        "GITHUB_CLIENT_SECRET": "",
        "JWT_SIGNING_KEY": "",
        "ALLOWED_GITHUB_LOGINS": {"def4alt"},
        "API_KEYS": {},
    }
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    store.init_db(db_path, views_dir=views_dir, vec_db_path=vec_path)
    return root, db_path, vec_path


@pytest.fixture
def server(env, monkeypatch: pytest.MonkeyPatch):
    _, _, _ = env
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(config, "PUBLIC_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setattr(config, "API_KEYS", {"opencode": "opencode-key"})
    app = uvicorn.Server(uvicorn.Config(build().http_app(path="/mcp"), port=port, log_level="error"))
    thread = threading.Thread(target=app.run, daemon=True)
    thread.start()
    while not app.started:
        time.sleep(0.05)
    yield config.PUBLIC_URL
    app.should_exit = True
    thread.join()


async def call(tool: str, **args) -> dict:
    async with Client(build()) as c:
        res = await c.call_tool(tool, args)
        return res.structured_content if res.structured_content is not None else res.data


def test_two_db_split_and_migrations_versioning(env, tmp_path: Path) -> None:
    _, db_path, vec_path = env
    with contextlib.closing(store.connect(db_path)) as conn:
        tables = {
            r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        assert tables == {"items", "links", "cues", "episodes", "episode_items", "cursors"}
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        triggers = conn.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'").fetchall()
        assert {r["tbl_name"] for r in triggers} == {"links"}
        assert {r["name"] for r in triggers} == {
            "trg_links_requires_no_cycle_ins",
            "trg_links_requires_no_cycle_upd",
        }

    with contextlib.closing(store.connect(vec_path)) as vconn:
        vec_tables = {
            r["name"] for r in vconn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        assert vec_tables == {"items_vec", "episodes_vec", "documents", "doc_chunks"}

    mdir = tmp_path / "migrations"
    shutil.copytree(store.MIGRATIONS_DIR, mdir)
    (mdir / "002-extra.sql").write_text("CREATE INDEX IF NOT EXISTS idx_items_updated ON items(updated_at);")
    with contextlib.closing(store.connect(db_path)) as conn:
        ver = store.apply_migrations(conn, migrations_dir=mdir)
        assert ver == 2
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert store.apply_migrations(conn, migrations_dir=mdir) == 2
        idx = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_items_updated'"
        ).fetchone()
        assert idx is not None


def test_requires_dag_trigger_blocks_cycles(env) -> None:
    _, db_path, _ = env
    with contextlib.closing(store.connect(db_path)) as conn:
        for item_id in ("a", "b", "c"):
            _insert_item(conn, item_id)
        conn.execute("INSERT INTO links(src, rel, dst) VALUES ('a', 'requires', 'b')")
        conn.execute("INSERT INTO links(src, rel, dst) VALUES ('b', 'requires', 'c')")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO links(src, rel, dst) VALUES ('c', 'requires', 'a')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO links(src, rel, dst) VALUES ('a', 'relates', 'a')")

        conn.execute("UPDATE links SET weight = 0.75 WHERE src = 'a' AND rel = 'requires' AND dst = 'b'")
        conn.execute("INSERT INTO links(src, rel, dst, status) VALUES ('c', 'requires', 'a', 'rejected')")

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE links SET status = 'active' WHERE src = 'c' AND rel = 'requires' AND dst = 'a'")

        conn.execute("INSERT INTO links(src, rel, dst) VALUES ('b', 'relates', 'a')")


def test_tutor_views_and_memory_views_with_cooccurrence_window(env) -> None:
    _, db_path, _ = env
    with contextlib.closing(store.connect(db_path)) as conn:
        for cid in (
            "c_unseen",
            "c_exposed",
            "c_learning",
            "c_acquired",
            "c_robust",
            "c_transferred",
            "c_contested",
            "c_due",
            "c_next",
            "c_blocked",
        ):
            _insert_item(conn, cid)
        _insert_item(conn, "mis-norm", kind="misconception", title="Raw Pixel DLT")
        _insert_item(conn, "homography")
        _insert_item(conn, "alex", kind="person", title="Alex")

        _insert_episode(conn, "exposure", _iso_days_ago(2.0), [("c_exposed", "about")])
        _insert_episode(conn, "partial", _iso_days_ago(0.2), [("c_learning", "tested")])
        _insert_episode(conn, "recall", _iso_days_ago(0.1), [("c_acquired", "tested")])
        _insert_episode(conn, "exposure", _iso_days_ago(4.0), [("c_robust", "about")])
        _insert_episode(conn, "recall", _iso_days_ago(3.0), [("c_robust", "tested")])
        _insert_episode(conn, "recall", _iso_days_ago(1.5), [("c_robust", "tested")])
        _insert_episode(conn, "recall", _iso_days_ago(4.0), [("c_transferred", "tested")])
        _insert_episode(conn, "recall", _iso_days_ago(2.5), [("c_transferred", "tested")])
        _insert_episode(conn, "transfer", _iso_days_ago(0.5), [("c_transferred", "tested")])
        _insert_episode(conn, "recall", _iso_days_ago(0.2), [("c_contested", "tested")])
        conn.execute("INSERT INTO links(src, rel, dst) VALUES ('mis-norm', 'contradicts', 'c_contested')")
        _insert_episode(conn, "recall", _iso_days_ago(10.0), [("c_due", "tested")])
        conn.execute("INSERT INTO links(src, rel, dst) VALUES ('c_acquired', 'requires', 'c_next')")
        conn.execute("INSERT INTO links(src, rel, dst) VALUES ('c_learning', 'requires', 'c_blocked')")

        standings = {
            r["item_id"]: dict(r)
            for r in conn.execute("SELECT * FROM v_concept_state").fetchall()
        }
        assert standings["c_unseen"]["standing"] == "unseen"
        assert standings["c_exposed"]["standing"] == "exposed"
        assert standings["c_learning"]["standing"] == "learning"
        assert standings["c_acquired"]["standing"] == "acquired"
        assert standings["c_robust"]["standing"] == "robust"
        assert standings["c_transferred"]["standing"] == "transferred"
        assert standings["c_contested"]["standing"] == "contested"
        assert standings["c_due"]["is_due"] == 1

        queue_ids = [r["item_id"] for r in conn.execute("SELECT item_id FROM v_review_queue").fetchall()]
        assert queue_ids[0] == "c_contested"
        assert "c_due" in queue_ids
        next_ids = {r["item_id"] for r in conn.execute("SELECT item_id FROM v_study_next").fetchall()}
        assert "c_next" in next_ids
        assert "c_blocked" not in next_ids

        conn.execute(
            "INSERT INTO cues(trigger_kind, item_id, due_at, action) VALUES ('time', 'alex', ?, 'Sync now')",
            (_iso_days_ago(0.1),),
        )
        conn.execute(
            "INSERT INTO cues(trigger_kind, item_id, due_at, action) VALUES ('time', 'alex', ?, 'Future cue')",
            (_iso_days_ago(-3.0),),
        )
        ep1 = _insert_episode(conn, "note", _iso_days_ago(0.2), [("alex", "person"), ("homography", "about")])
        ep2 = _insert_episode(conn, "miss", _iso_days_ago(0.1), [("alex", "person"), ("homography", "tested")])
        _insert_episode(conn, "sleep", _iso_days_ago(0.05), [("homography", "about")], source="sleep")
        conn.execute("INSERT INTO cursors(name, pos) VALUES ('sleep:nrem', ?)", (str(ep1),))
        conn.execute("INSERT INTO cursors(name, pos) VALUES ('sleep:shy', ?)", (str(ep1),))

        due = [r["action"] for r in conn.execute("SELECT action FROM v_due_cues").fetchall()]
        assert due == ["Sync now"]
        uncons = [r["id"] for r in conn.execute("SELECT id FROM v_unconsolidated_episodes").fetchall()]
        assert uncons == [ep2]
        co = conn.execute("SELECT item_a, item_b, co_count FROM v_cooccurrence").fetchone()
        assert (co["item_a"], co["item_b"], co["co_count"]) == ("alex", "homography", 2)
        win = conn.execute("SELECT item_a, item_b, co_count FROM v_cooccurrence_window").fetchone()
        assert (win["item_a"], win["item_b"], win["co_count"]) == ("alex", "homography", 1)
        active = {r["id"]: r for r in conn.execute("SELECT * FROM v_active_items").fetchall()}
        assert active["homography"]["episode_count"] > 0
        assert active["homography"]["base_level"] is not None


async def test_path_jail_and_live_view_reload(env) -> None:
    root, _, _ = env
    pdf_bytes = b"%PDF-1.4\nfake-raw-pdf-bytes"
    first = uploads.store_bytes(pdf_bytes, "scan.pdf")
    second = uploads.store_bytes(pdf_bytes, "SCAN.PDF")
    assert not first["duplicate"] and second["duplicate"]
    assert first["path"] == second["path"]

    meta = files.read_file(root, str(first["path"]))
    assert meta["binary"] is True

    files.write_file(root, "notes/homography.md", "A" * 20)
    page = files.read_file(root, "notes/homography.md", offset=0, limit=8)
    assert page["truncated"] is True and page["text"] == "AAAAAAAA" and page["next_offset"] == 8

    for bad in ("../outside.txt", "backups/vault.db", "blobs/overwrite.bin"):
        with pytest.raises(ValueError):
            files.write_file(root, bad, "bad")

    view_sql = (
        "DROP VIEW IF EXISTS v_custom_people;\n"
        "CREATE VIEW v_custom_people AS SELECT id FROM items WHERE kind = 'person';"
    )
    res = await call("file", action="write", path="usr/views/custom.sql", content=view_sql)
    assert res["path"] == "usr/views/custom.sql"
    res_q = await call("sql", query="SELECT id FROM v_custom_people")
    assert res_q["rows"] == []

    del_res = await call("file", action="delete", path="usr/views/custom.sql")
    assert del_res["deleted"] is True


def test_sql_authorizer_int8_vectors_returning_and_rebuild(env, monkeypatch: pytest.MonkeyPatch) -> None:
    _, db_path, vec_path = env
    for bad_sql in (
        "ATTACH ':memory:' AS other",
        f"VACUUM INTO '{db_path.parent / 'steal.db'}'",
        "DROP TABLE items",
    ):
        with pytest.raises(ValueError):
            store.run_sql(db_path, bad_sql, vec_db_path=vec_path)

    for i in range(120):
        store.run_sql(
            db_path,
            "INSERT INTO items(id, kind, title, body) VALUES (?, 'concept', ?, ?)",
            [f"item-{i:03d}", f"Concept {i}", f"Notes about topic {i} and projective matrix"],
            vec_db_path=vec_path,
        )

    ret = store.run_sql(
        db_path,
        "INSERT INTO items(id, kind, title, body) "
        "VALUES ('ret-item', 'concept', 'Returned', 'Quaternion rotation') RETURNING id",
        vec_db_path=vec_path,
    )
    assert ret["rows"] == [{"id": "ret-item"}]
    with contextlib.closing(store.connect(vec_path)) as vconn:
        assert vconn.execute("SELECT 1 FROM items_vec WHERE item_id = 'ret-item'").fetchone() is not None

    bad_vec_insert = (
        "INSERT INTO vec.items_vec(item_id, model, content_hash, embedding) "
        "VALUES ('bad', 'x', zeroblob(16), zeroblob(260))"
    )
    with pytest.raises(ValueError):
        store.run_sql(db_path, bad_vec_insert, vec_db_path=vec_path)
    with pytest.raises(ValueError):
        store.run_sql(db_path, "DELETE FROM vec.items_vec WHERE item_id = 'item-000'", vec_db_path=vec_path)

    p1 = store.run_sql(db_path, "SELECT id FROM items ORDER BY id", limit=100, vec_db_path=vec_path)
    assert len(p1["rows"]) == 100 and p1["truncated"] is True
    p2 = store.run_sql(
        db_path, "SELECT id FROM items ORDER BY id", limit=100, cursor=p1["next_cursor"], vec_db_path=vec_path
    )
    assert len(p2["rows"]) == 21 and p2["truncated"] is False

    vec_query = (
        "SELECT item_id, cosine(embedding, embed('projective matrix')) AS sim "
        "FROM vec.items_vec ORDER BY sim DESC LIMIT 3"
    )
    vec_res = store.run_sql(db_path, vec_query, vec_db_path=vec_path)
    assert len(vec_res["rows"]) == 3 and vec_res["rows"][0]["sim"] > 0.0

    with contextlib.closing(store.connect(vec_path)) as vconn:
        row0 = vconn.execute("SELECT content_hash, embedding FROM items_vec WHERE item_id = 'item-000'").fetchone()
        assert len(row0["content_hash"]) == 16
        assert len(row0["embedding"]) == embed.INT8_HEADER.size + embed.HASH_DIM
        before_blob = bytes(row0["embedding"])

    store.run_sql(
        db_path,
        "UPDATE items SET body = 'Completely rewritten orbital mechanics note' WHERE id = 'item-000'",
        vec_db_path=vec_path,
    )
    store.run_sql(db_path, "DELETE FROM items WHERE id = 'item-001'", vec_db_path=vec_path)

    with contextlib.closing(store.connect(vec_path)) as vconn:
        after_blob = bytes(vconn.execute("SELECT embedding FROM items_vec WHERE item_id = 'item-000'").fetchone()[0])
        assert before_blob != after_blob
        assert vconn.execute("SELECT 1 FROM items_vec WHERE item_id = 'item-001'").fetchone() is None
        assert vconn.execute("SELECT COUNT(*) FROM items_vec").fetchone()[0] == 120

    vec_path.unlink()
    monkeypatch.setattr(config, "DB_PATH", db_path)
    store.rebuild_cli()
    with contextlib.closing(store.connect(vec_path)) as vconn:
        assert vconn.execute("SELECT COUNT(*) FROM items_vec").fetchone()[0] == 120


async def test_recall_and_resources_over_fastmcp(env) -> None:
    await call(
        "sql",
        query=(
            "INSERT INTO items(id, kind, title, body) VALUES "
            "('svd-nullspace', 'concept', 'SVD Nullspace', 'Smallest singular vector solves Ax=0')"
        ),
    )
    await call(
        "sql",
        query=(
            "INSERT INTO items(id, kind, title, body) VALUES "
            "('homography', 'concept', 'Homography', 'Planar 3x3 DLT warp; unnormalized pixels raise ERR_DLT_491')"
        ),
    )
    await call(
        "sql",
        query=(
            "INSERT INTO items(id, kind, title, body) VALUES "
            "('robotics-exam', 'goal', 'Robotics Exam', 'Prepare final evaluation checklist')"
        ),
    )
    await call(
        "sql",
        query="INSERT INTO links(src, rel, dst, weight) VALUES ('svd-nullspace', 'requires', 'homography', 0.9)",
    )
    await call(
        "sql",
        query="INSERT INTO links(src, rel, dst, weight) VALUES ('homography', 'requires', 'robotics-exam', 0.85)",
    )
    await call(
        "sql",
        query=(
            "INSERT INTO cues(trigger_kind, on_item_id, action) "
            "VALUES ('event', 'homography', 'Check Hartley normalization first')"
        ),
    )
    ep_ret = await call(
        "sql",
        query=(
            "INSERT INTO episodes(kind, source, context, detail) "
            "VALUES ('miss', 'assistant', 'runway-pose.py', 'Telemetry freeze during landing flare') RETURNING id"
        ),
    )
    ep_id = ep_ret["rows"][0]["id"]
    await call(
        "sql",
        query="INSERT INTO episode_items(episode_id, item_id, role) VALUES (?, 'homography', 'tested')",
        params=[ep_id],
    )

    opaque = await call("recall", query="ERR_DLT_491 pixel coords", budget=1800, limit=4)
    assert opaque["items"][0]["id"] == "homography"
    assert opaque["items"][0]["score"]["opaque"] > 0.0
    assert len(opaque["cues_fired"]) == 1

    ep_pkt = await call("recall", query="telemetry freeze landing flare", budget=1800, limit=1)
    assert [e["id"] for e in ep_pkt["episodes"]] == [ep_id]
    assert "homography" in [it["id"] for it in ep_pkt["items"]]

    exam_pkt = await call("recall", query="robotics exam", budget=1800, limit=1)
    assert exam_pkt["items"][0]["id"] == "robotics-exam"
    assert "homography" in {it["id"] for it in exam_pkt["attached_items"]}
    assert exam_pkt["approx_tokens"] <= 1800

    async with Client(build()) as c:
        schema_parts = await c.read_resource("vault://schema")
        views_parts = await c.read_resource("vault://views")
        assert "CREATE TABLE IF NOT EXISTS items" in schema_parts[0].text
        assert "v_study_next" in views_parts[0].text


async def test_auth_keys_gatekeeper_and_live_http(server: str, monkeypatch: pytest.MonkeyPatch) -> None:
    verifier = ApiKeys()
    good = await verifier.verify_token("opencode-key")
    bad = await verifier.verify_token("wrong")
    assert good is not None and good.subject == "opencode" and bad is None
    assert build_auth() is not None

    with monkeypatch.context() as mp:
        mp.setattr(
            "vault_mcp.app.auth.get_access_token",
            lambda: type("T", (), {"claims": {"login": "stranger"}})(),
        )
        gk = Gatekeeper()
        with pytest.raises(ToolError):
            await gk.on_request(None, lambda ctx: None)

    hz = httpx2.get(f"{server}/healthz", timeout=5)
    assert hz.status_code == 200 and hz.text.strip() == "ok"

    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
    }
    headers = {"Accept": "application/json, text/event-stream"}
    assert httpx2.post(f"{server}/mcp", json=init, headers=headers, timeout=5).status_code == 401
    assert (
        httpx2.post(
            f"{server}/mcp",
            json=init,
            headers={**headers, "Authorization": "Bearer wrong-key"},
            timeout=5,
        ).status_code
        == 401
    )

    transport = StreamableHttpTransport(f"{server}/mcp", headers={"Authorization": "Bearer opencode-key"})
    async with Client(transport) as client:
        tools = {t.name for t in await client.list_tools()}
        assert tools == {"sql", "recall", "file", "document"}
        res = await client.call_tool("sql", {"query": "SELECT 1 AS ok"})
        data = res.structured_content or res.data
        assert data["rows"] == [{"ok": 1}]


async def test_documents_index_read_recall_and_signed_links(env, tmp_path: Path, monkeypatch) -> None:
    root, db_path, vec_path = env
    docs = config.DOCS_ROOT
    (docs / "University").mkdir(parents=True)
    (docs / ".stversions").mkdir()
    (docs / "University" / "thesis-proposal.md").write_text(
        "# Proposal\n\nMonocular drone view 3D object detection with depth priors.\n\n"
        + "\n\n".join(f"Paragraph {i} about evaluation protocols." for i in range(40))
    )
    (docs / "Personal").mkdir()
    (docs / "Personal" / "page.html").write_text("<script>alert(1)</script>")
    (docs / ".stversions" / "old.md").write_text("hidden version")

    assert documents.sync(vec_path) == 1
    assert documents.sync(vec_path) == 0  # unchanged files are skipped
    listed = await call("sql", query="SELECT path, folder, chunks FROM vec.documents")
    assert listed["rows"] == [{"path": "University/thesis-proposal.md", "folder": "University", "chunks": 2}]

    doc = await call("document", path="University/thesis-proposal.md", limit=30)
    assert doc["text"].startswith("# Proposal") and doc["truncated"] and doc["next_offset"] == 30
    assert signing.verify(doc["url"].removeprefix(config.PUBLIC_URL)) == 200
    with pytest.raises(ToolError):
        await call("document", path="../vault/usr/x.sql")
    with pytest.raises(ToolError):
        await call("document", path=".stversions/old.md")

    monkeypatch.setattr(store, "DOC_MIN_SIMILARITY", 0.05)  # hashed test embeddings score lower than Harrier
    pkt = await call("recall", query="monocular drone 3D object detection depth priors", budget=4000)
    assert pkt["documents"][0]["path"] == "University/thesis-proposal.md"
    assert "url" in pkt["documents"][0]

    (docs / "University" / "thesis-proposal.md").unlink()
    assert documents.sync(vec_path) == 1
    assert (await call("sql", query="SELECT count(*) AS n FROM vec.doc_chunks"))["rows"] == [{"n": 0}]


async def test_blob_links_and_verify(env) -> None:
    root, _, _ = env
    put = uploads.store_bytes(b"%PDF-1.4 fake", "x.pdf")
    res = await call("file", action="read", path=put["path"])
    assert res["binary"] and res["url"].startswith(f"{config.PUBLIC_URL}/f/")
    good = res["url"].removeprefix(config.PUBLIC_URL)
    assert signing.verify(good) == 200
    assert signing.verify(good.replace("s=", "s=x")) == 403
    assert signing.verify("/f/" + "0" * 16 + "/x?e=1&s=abc") == 403
    expired = signing.blob_link(put["path"], "x.pdf", ttl=-10).removeprefix(config.PUBLIC_URL)
    assert signing.verify(expired) == 410


async def test_long_body_round_trips_through_sql_unclipped(env) -> None:
    body = "## Seen\n\n" + "\n".join(f"- wg-{i:05d}" for i in range(2000))  # ~22 KB, like a long watch list
    await call("sql", query="INSERT INTO items(id, kind, title, body) VALUES ('watch', 'goal', 'Watch', ?)",
               params=[body])
    got = (await call("sql", query="SELECT body FROM items WHERE id = 'watch'"))["rows"][0]["body"]
    assert got == body


async def test_upload_links_stream_into_blobs(server: str, env, monkeypatch) -> None:
    root, _, _ = env
    transport = StreamableHttpTransport(f"{server}/mcp", headers={"Authorization": "Bearer opencode-key"})
    async with Client(transport) as client:
        res = await client.call_tool("file", {"action": "upload_link", "name": "Lio contract.pdf"})
        link = res.structured_content or res.data
    assert link["curl"].startswith("curl -fsS -T 'Lio contract.pdf' ")
    target = link["put_url"].replace("{filename}", "Lio%20contract.pdf")
    body = b"%PDF-1.7 " + b"x" * 300_000

    up = httpx2.put(target, content=body, timeout=10)
    assert up.status_code == 201, up.text
    info = up.json()
    assert info["path"].startswith("blobs/") and info["path"].endswith(".pdf")
    assert info["size"] == len(body) and info["mime"] == "application/pdf" and info["filename"] == "Lio contract.pdf"
    assert (root / info["path"]).read_bytes() == body
    assert signing.verify(info["url"].removeprefix(config.PUBLIC_URL)) == 200
    assert not list((root / ".incoming").glob("*.part"))

    again = httpx2.put(link["put_url"].replace("{filename}", "copy.pdf"), content=body, timeout=10)
    assert again.status_code == 200 and again.json()["duplicate"]  # one link, many files; same bytes, same blob

    forged = target.replace("s=", "s=AA")
    assert httpx2.put(forged, content=b"x", timeout=10).status_code == 403
    expired, _ = signing.upload_link("ab" * 12, ttl=-5)
    assert httpx2.put(expired.replace("{filename}", "x.txt"), content=b"x", timeout=10).status_code == 410

    monkeypatch.setattr(config, "UPLOAD_MAX_BYTES", 1000)
    assert httpx2.put(target, content=b"y" * 5000, timeout=10).status_code == 413
    monkeypatch.setattr(config, "UPLOAD_MAX_BYTES", 10**9)
    monkeypatch.setattr(config, "QUOTA_BYTES", 400_000)
    assert httpx2.put(target, content=b"z" * 200_000, timeout=10).status_code == 413


async def test_fetch_refuses_private_addresses(env) -> None:
    for url in ("http://127.0.0.1:8000/healthz", "http://localhost/x", "file:///etc/passwd", "http://10.0.0.1/a"):
        with pytest.raises(ToolError):
            await call("file", action="fetch", url=url)
