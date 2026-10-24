"""Agent port listener: two sockets, one server, path gate by accepting port."""

from __future__ import annotations

import asyncio
import socket

import httpx
import pytest
from fastapi import FastAPI

from agent_orchestrator import cli
from agent_orchestrator.api.allowlist import PortGate, SourceAllowlist
from agent_orchestrator.settings import Settings

DB = "postgresql://u:p@h/db"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    import os

    for k in list(os.environ):
        if k.startswith("ORCH_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("ORCH_DATABASE_URL", DB)


def make_app() -> FastAPI:
    import ipaddress

    app = FastAPI()

    @app.get("/agent/v1/ping")
    async def ping():
        return {"ok": True}

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/api/v1/x")
    async def x():
        return {"x": 1}

    app.add_middleware(
        SourceAllowlist, networks=[ipaddress.ip_network("10.0.0.0/8")], skip_port=8001
    )
    app.add_middleware(PortGate, agent_port=8001)
    return app


async def get(url: str, client=("203.0.113.9", 1234)):
    transport = httpx.ASGITransport(app=make_app(), client=client)
    async with httpx.AsyncClient(transport=transport) as c:
        return await c.get(url)


def test_agent_settings(monkeypatch):
    s = Settings()
    assert s.agent_port == 8001
    assert s.agent_api_url is None
    monkeypatch.setenv("ORCH_AGENT_PORT", "9001")
    monkeypatch.setenv("ORCH_AGENT_API_URL", "http://agents.ns:8001")
    s = Settings()
    assert s.agent_port == 9001
    assert s.agent_api_url == "http://agents.ns:8001"


def test_agent_port_serves_only_agent_paths():
    async def run():
        ok = await get("http://h:8001/agent/v1/ping")
        health = await get("http://h:8001/healthz")
        other = await get("http://h:8001/api/v1/x")
        return ok, health, other

    ok, health, other = asyncio.run(run())
    assert ok.status_code == 200  # source 203.0.113.9 is outside the allowlist
    assert health.status_code == 200
    assert other.status_code == 404
    assert other.json()["error"]["code"] == "not_found"


def test_main_port_unchanged():
    async def run():
        return (
            await get("http://h:8000/agent/v1/ping", client=("10.1.1.1", 1)),
            await get("http://h:8000/api/v1/x", client=("203.0.113.9", 1)),
            await get("http://h:8000/api/v1/x", client=("10.1.1.1", 1)),
        )

    agent, refused, allowed = asyncio.run(run())
    assert agent.status_code == 404
    assert agent.json()["error"]["code"] == "not_found"
    assert refused.status_code == 403
    assert allowed.status_code == 200


def test_two_sockets_bound():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        a = probe.getsockname()[1]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        b = probe.getsockname()[1]
    settings = Settings(http_host="127.0.0.1", http_port=a, agent_port=b)
    socks = cli.bind_sockets(settings)
    try:
        assert sorted(s.getsockname()[1] for s in socks) == sorted([a, b])
        config = cli.build_uvicorn_config(settings)
        assert config.proxy_headers is False
    finally:
        for s in socks:
            s.close()
