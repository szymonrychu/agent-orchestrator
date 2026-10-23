"""Clusters and agents routes, error shape, source allowlist and the layering rule."""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from factories import insert_agent, insert_cluster, insert_message, set_agent_status

import agent_orchestrator.api as api_pkg
from agent_orchestrator.api.app import create_app
from agent_orchestrator.db import clusters as repo
from agent_orchestrator.settings import Settings

PEER = ("10.1.2.3", 5555)


def make_settings(dsn: str = "postgresql://unused/unused", **kw) -> Settings:
    kw.setdefault("allowed_cidrs", "10.1.2.0/24")
    kw.setdefault("agent_image", "img:1")
    return Settings(database_url=dsn, **kw)


def client_for(app, peer=PEER, **kw) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=peer),
        base_url="http://orch",
        **kw,
    )


@pytest_asyncio.fixture
async def http(pool, dsn):
    app = create_app(make_settings(dsn), pool)
    async with client_for(app) as c:
        yield c


def assert_error(resp, status, code):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)


async def test_create_and_get_cluster(http):
    r = await http.post("/api/v1/clusters", json={"slug": "alpha", "spec": {"replicas": 2}})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["generation"] == 1
    assert created["slug"] == "alpha"
    got = (await http.get(f"/api/v1/clusters/{created['id']}")).json()
    assert got["generation"] == 1
    assert got["observed_generation"] == 0
    assert got["desired"] == "active"
    assert got["spec"]["replicas"] == 2
    assert got["summary"]["replicas"] == 2
    assert got["summary"]["state"] == "booting"
    listed = (await http.get("/api/v1/clusters")).json()
    assert [c["slug"] for c in listed] == ["alpha"]


@pytest.mark.parametrize(
    "body",
    [
        {"slug": "Bad_Slug", "spec": {}},
        {"slug": "orchestrator-x", "spec": {}},
        {"slug": "ok", "spec": {"replicas": 0}},
        {"slug": "ok", "spec": {"nope": 1}},
        {"spec": {}},
    ],
)
async def test_create_rejects_bad_slug_422(http, body):
    assert_error(await http.post("/api/v1/clusters", json=body), 422, "invalid_request")


async def test_duplicate_slug_409(http):
    body = {"slug": "dup", "spec": {}}
    assert (await http.post("/api/v1/clusters", json=body)).status_code == 201
    assert_error(await http.post("/api/v1/clusters", json=body), 409, "conflict")


async def test_patch_bumps_generation(http):
    cid = (await http.post("/api/v1/clusters", json={"slug": "p", "spec": {}})).json()["id"]
    r = await http.patch(f"/api/v1/clusters/{cid}", json={"spec": {"replicas": 3}})
    assert r.status_code == 200, r.text
    assert r.json()["generation"] == 2
    assert r.json()["spec"]["replicas"] == 3
    assert_error(
        await http.patch(f"/api/v1/clusters/{uuid.uuid4()}", json={"spec": {}}), 404, "not_found"
    )


async def test_delete_is_discard_without_purge(http, pool):
    cid = (await http.post("/api/v1/clusters", json={"slug": "d", "spec": {}})).json()["id"]
    r = await http.delete(f"/api/v1/clusters/{cid}")
    assert r.status_code == 202
    assert r.json()["desired"] == "deleting"
    assert r.json()["summary"]["state"] == "deleting"
    again = await http.delete(f"/api/v1/clusters/{cid}")
    assert again.status_code == 202
    assert again.json()["desired"] == "deleting"
    assert_error(await http.delete(f"/api/v1/clusters/{cid}?purge=true"), 422, "invalid_request")
    assert_error(
        await http.patch(f"/api/v1/clusters/{cid}", json={"spec": {}}), 409, "cluster_deleting"
    )
    async with pool.connection() as conn:
        await repo.purge(conn, uuid.UUID(cid))
    assert_error(await http.get(f"/api/v1/clusters/{cid}"), 404, "not_found")
    assert_error(await http.delete(f"/api/v1/clusters/{cid}"), 404, "not_found")


async def test_agents_view_joins_status(http, pool):
    c = await insert_cluster(pool, spec={"replicas": 2})
    a0 = await insert_agent(pool, c["id"], ready=True, pod_name="web-0")
    await insert_agent(pool, c["id"], ready=False, pod_name="web-1")
    await insert_agent(pool, c["id"], ready=False, pod_name="web-2", gone_at=None)
    await set_agent_status(
        pool,
        a0["id"],
        state="busy",
        current_turn_id="t1",
        context_used_tokens=1000,
        context_window_tokens=4000,
        context_pct=25.0,
        peers=[{"name": "x"}, {"name": "y"}],
    )
    r = await http.get(f"/api/v1/clusters/{c['id']}/agents")
    assert r.status_code == 200
    agents = r.json()
    assert [a["ordinal"] for a in agents] == [0, 1, 2]
    first = agents[0]
    assert first["pod_name"] == "web-0"
    assert first["ready"] is True
    assert first["state"] == "busy"
    assert first["current_turn_id"] == "t1"
    assert first["context"] == {"used_tokens": 1000, "window_tokens": 4000, "used_percentage": 25.0}
    assert first["peers"] == 2
    assert first["gone"] is False
    assert first["free_since"] is not None
    assert agents[1]["state"] == "unknown"
    assert agents[1]["context"] is None
    assert agents[1]["peers"] == 0
    assert_error(await http.get(f"/api/v1/clusters/{uuid.uuid4()}/agents"), 404, "not_found")


async def test_cluster_summary_state_and_gauge_max(http, pool):
    c = await insert_cluster(pool, spec={"replicas": 3, "auto_compact_pct": 80})
    a0 = await insert_agent(pool, c["id"], ready=True)
    a1 = await insert_agent(pool, c["id"], ready=True)
    await insert_agent(pool, c["id"], ready=False)
    await set_agent_status(pool, a0["id"], state="idle", context_pct=30.0)
    await set_agent_status(pool, a1["id"], state="idle", context_pct=62.5)
    await insert_message(pool, c["id"], status="queued")
    await insert_message(pool, c["id"], status="queued")
    await insert_message(pool, c["id"], status="failed")
    summary = (await http.get(f"/api/v1/clusters/{c['id']}")).json()["summary"]
    assert summary == {
        "state": "waiting",
        "ready": 2,
        "replicas": 3,
        "context_pct_max": 62.5,
        "auto_compact_pct": 80,
        "queued": 2,
        "failed": 1,
    }
    await set_agent_status(pool, a1["id"], state="busy", context_pct=62.5)
    summary = (await http.get("/api/v1/clusters")).json()[0]["summary"]
    assert summary["state"] == "working"

    empty = await insert_cluster(pool, spec={})
    got = (await http.get(f"/api/v1/clusters/{empty['id']}")).json()["summary"]
    assert got["context_pct_max"] is None
    assert got["state"] == "booting"
    assert got["auto_compact_pct"] == 95

    bad = await insert_cluster(pool, spec={}, status={"error": "boom"})
    assert (await http.get(f"/api/v1/clusters/{bad['id']}")).json()["summary"]["state"] == "error"


async def test_cluster_view_exposes_discard_hook_state(http, pool):
    c = await insert_cluster(pool, desired="deleting", status={"discard_hook_failed": "http 500"})
    view = (await http.get(f"/api/v1/clusters/{c['id']}")).json()
    assert view["status"]["discard_hook_failed"] == "http 500"
    assert view["summary"]["state"] == "deleting"


async def test_errors_use_wrapper_shape(http):
    assert_error(await http.get(f"/api/v1/clusters/{uuid.uuid4()}"), 404, "not_found")
    assert_error(await http.get("/api/v1/clusters/not-a-uuid"), 422, "invalid_request")
    assert_error(await http.get("/api/v1/nothing-here"), 404, "not_found")
    assert_error(await http.post("/api/v1/clusters", content=b"{"), 422, "invalid_request")


async def test_unexpected_error_is_internal(pool, dsn):
    app = create_app(make_settings(dsn), pool)

    @app.get("/api/v1/boom")
    async def boom():
        raise RuntimeError("x")

    transport = httpx.ASGITransport(app=app, client=PEER, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://orch") as c:
        assert_error(await c.get("/api/v1/boom"), 500, "internal")


def test_api_never_imports_k8s_or_wrapper():
    banned = (
        "agent_orchestrator.k8s",
        "agent_orchestrator.wrapper",
        "agent_orchestrator.reconciler",
    )
    files = list(Path(api_pkg.__file__).parent.glob("*.py"))
    assert len(files) >= 6
    for f in files:
        for node in ast.walk(ast.parse(f.read_text())):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = [mod] + [f"{mod}.{a.name}" for a in node.names]
                if node.level and mod in ("k8s", "wrapper", "reconciler"):
                    names.append("agent_orchestrator." + mod)
            for n in names:
                assert not n.startswith(banned), f"{f.name} imports {n}"


async def test_meta_defaults(pool, dsn):
    settings = make_settings(
        dsn,
        default_model="m1",
        default_effort="high",
        models="m1,m2",
        agent_images="img:2",
        max_replicas=4,
        max_pvc_size="50Gi",
        max_clusters=7,
        discard_hook_url="https://secret.example/hook",
    )
    async with client_for(create_app(settings, pool)) as c:
        meta = (await c.get("/api/v1/meta")).json()
    assert "secret.example" not in str(meta)
    assert meta["defaults"] == {
        "model": "m1",
        "effort": "high",
        "replicas": 1,
        "auto_compact_pct": 95,
        "pvc_size": "20Gi",
        "storage_class": "rook-ceph-rwx",
        "max_attempts": 5,
    }
    assert meta["agent_image"] == "img:1"
    assert meta["agent_images"] == ["img:1", "img:2"]
    assert meta["models"] == ["m1", "m2"]
    assert meta["caps"] == {"max_replicas": 4, "max_pvc_size": "50Gi", "max_clusters": 7}
    assert meta["discard_hook"] == "url"

    async with client_for(create_app(make_settings(dsn), pool)) as c:
        assert (await c.get("/api/v1/meta")).json()["discard_hook"] == "none"
    cmd = make_settings(dsn, discard_hook_command="/bin/export")
    async with client_for(create_app(cmd, pool)) as c:
        meta = (await c.get("/api/v1/meta")).json()
    assert meta["discard_hook"] == "command"
    assert "/bin/export" not in str(meta)


async def test_foreign_peer_forbidden_except_health(pool, dsn):
    app = create_app(make_settings(dsn), pool)
    async with client_for(app, peer=("10.9.9.9", 1)) as c:
        for method, path in [
            ("GET", "/api/v1/clusters"),
            ("POST", "/api/v1/clusters"),
            ("GET", "/api/v1/meta"),
            ("GET", "/api/v1/openapi.json"),
            ("GET", "/api/v1/docs"),
            ("GET", "/anything"),
        ]:
            assert_error(await c.request(method, path), 403, "forbidden")
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 200
    async with client_for(app, peer=("10.1.2.9", 1)) as c:
        assert (await c.get("/api/v1/clusters")).status_code == 200
        assert (await c.get("/api/v1/openapi.json")).status_code == 200
    async with client_for(app, peer=("::ffff:10.1.2.9", 1)) as c:
        assert (await c.get("/api/v1/clusters")).status_code == 200
    async with client_for(app, peer=("not-an-ip", 1)) as c:
        assert_error(await c.get("/api/v1/clusters"), 403, "forbidden")


async def test_forwarded_headers_ignored_by_allowlist(pool, dsn):
    app = create_app(make_settings(dsn), pool)
    spoof = {
        "X-Forwarded-For": "10.1.2.9",
        "X-Real-IP": "10.1.2.9",
        "Forwarded": "for=10.1.2.9",
    }
    async with client_for(app, peer=("172.16.0.5", 1), headers=spoof) as c:
        assert_error(await c.get("/api/v1/clusters"), 403, "forbidden")
    async with client_for(app, peer=("10.1.2.9", 1), headers={"X-Forwarded-For": "8.8.8.8"}) as c:
        assert (await c.get("/api/v1/clusters")).status_code == 200


async def test_health_and_readiness(pool, dsn):
    app = create_app(make_settings(dsn), pool)
    async with client_for(app) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert (await c.get("/readyz")).status_code == 200
    await pool.close()
    async with client_for(app) as c:
        assert (await c.get("/healthz")).status_code == 200
        assert_error(await c.get("/readyz"), 503, "internal")


async def test_lifespan_opens_and_closes_own_pool(dsn):
    app = create_app(make_settings(dsn))
    async with app.router.lifespan_context(app):
        async with client_for(app) as c:
            assert (await c.get("/readyz")).status_code == 200
            assert (await c.get("/api/v1/clusters")).json() == []
        pool = app.state.pool
    assert pool.closed


async def test_openapi_and_docs_paths(http):
    assert (await http.get("/api/v1/openapi.json")).json()["info"]["title"]
    assert (await http.get("/api/v1/docs")).status_code == 200


@pytest.mark.parametrize(
    "spec",
    [
        {"image": "evil:latest"},
        {"env": {"CCW_TOKEN": "x"}},
        {"env": {"HTTPS_PROXY": "x"}},
        {"replicas": 5},
        {"pvc_size": "200Gi"},
        {"any_mcp_config": {"plugins": "nope"}},
    ],
)
async def test_spec_hardening_enforced_on_create_and_patch(pool, dsn, spec):
    app = create_app(make_settings(dsn, max_replicas=4, max_pvc_size="100Gi"), pool)
    async with client_for(app) as c:
        assert_error(
            await c.post("/api/v1/clusters", json={"slug": "h", "spec": spec}),
            422,
            "invalid_request",
        )
        cid = (await c.post("/api/v1/clusters", json={"slug": "ok", "spec": {}})).json()["id"]
        assert_error(
            await c.patch(f"/api/v1/clusters/{cid}", json={"spec": spec}), 422, "invalid_request"
        )
        assert (await c.get(f"/api/v1/clusters/{cid}")).json()["generation"] == 1
        ok = await c.post("/api/v1/clusters", json={"slug": "fine", "spec": {"image": "img:1"}})
        assert ok.status_code == 201


async def test_cluster_count_cap_409(pool, dsn):
    app = create_app(make_settings(dsn, max_clusters=2), pool)
    async with client_for(app) as c:
        for slug in ("one", "two"):
            assert (
                await c.post("/api/v1/clusters", json={"slug": slug, "spec": {}})
            ).status_code == 201
        assert_error(
            await c.post("/api/v1/clusters", json={"slug": "three", "spec": {}}),
            409,
            "cluster_limit",
        )
