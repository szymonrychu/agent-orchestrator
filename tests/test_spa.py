"""SPA static serving (D12)."""

from __future__ import annotations

import pytest
from test_api_clusters import client_for, make_settings

from agent_orchestrator.api.app import create_app


@pytest.fixture
def static_dir(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>spa</html>")
    (tmp_path / "assets" / "app-abc123.js").write_text("console.log(1)")
    return tmp_path


def _client(static: str):
    return client_for(create_app(make_settings(static_dir=static)))


async def test_index_no_cache_assets_immutable(static_dir):
    async with _client(str(static_dir)) as c:
        r = await c.get("/")
        assert r.status_code == 200
        assert "spa" in r.text
        assert r.headers["cache-control"] == "no-cache"
        r = await c.get("/index.html")
        assert r.headers["cache-control"] == "no-cache"
        r = await c.get("/assets/app-abc123.js")
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


async def test_client_route_fallback(static_dir):
    async with _client(str(static_dir)) as c:
        r = await c.get("/clusters/abc/agents/1")
        assert r.status_code == 200
        assert "spa" in r.text
        assert r.headers["cache-control"] == "no-cache"
        r = await c.get("/assets/missing.js")
        assert r.status_code == 404


async def test_unknown_api_path_is_json_404(static_dir):
    async with _client(str(static_dir)) as c:
        r = await c.get("/api/v1/nope")
        assert r.status_code == 404
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["error"]["code"] == "not_found"
        r = await c.get("/healthz")
        assert r.json() == {"status": "ok"}


@pytest.mark.parametrize("kind", ["unset", "missing"])
async def test_no_static_dir_no_mount(kind, tmp_path):
    static = "" if kind == "unset" else str(tmp_path / "absent")
    async with _client(static) as c:
        r = await c.get("/")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "not_found"


async def test_patch_cluster_405_with_spa_mounted(static_dir):
    async with _client(str(static_dir)) as c:
        r = await c.patch("/api/v1/clusters/6f1c2f0e-0000-4000-8000-000000000001", json={})
        assert r.status_code == 405
        assert r.headers["allow"] == "GET, HEAD"
        r = await c.post("/api/v1/nope", json={})
        assert r.status_code == 405


async def test_unknown_api_get_404_and_index_served(static_dir):
    async with _client(str(static_dir)) as c:
        r = await c.get("/api/v1/unknown")
        assert r.status_code == 404
        r = await c.head("/api/v1/unknown")
        assert r.status_code == 404
        r = await c.get("/")
        assert r.status_code == 200
        assert "spa" in r.text
