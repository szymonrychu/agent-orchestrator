"""Agent scale route: POST /agent/v1/scale (leader only) and the any-mcp plugin contract."""

from __future__ import annotations

import json

import httpx
from any_mcp.orchestrator import OrchestratorPlugin
from any_mcp.testing import PluginHarness
from factories import insert_agent, insert_session
from test_api_agent import (
    AGENT_PORT,
    IP0,
    IP1,
    client_for,
    headers,
    make_settings,
    set_ip,
    token,
)
from test_api_sessions import assert_error

from agents_orchestrator.api.app import create_app


async def hive(pool, *, replicas: int = 3, max_replicas: int = 4, static: bool = False):
    spec = {"replicas": replicas, "max_replicas": max_replicas, "static": static}
    c = await insert_session(pool, spec=spec)
    a0 = await insert_agent(pool, c["id"], ordinal=0, ready=True)
    a1 = await insert_agent(pool, c["id"], ordinal=1, ready=True)
    await set_ip(pool, a0["id"], IP0)
    await set_ip(pool, a1["id"], IP1)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO session_leaders (session_id, agent_id, epoch, since, reason)"
            " VALUES (%s, %s, 1, now(), 'start')",
            (c["id"], a0["id"]),
        )
    return c, a0, a1


async def replicas_of(pool, session_id) -> int:
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT (spec->>'replicas')::int FROM sessions WHERE id = %s", (session_id,)
        )
        return (await cur.fetchone())[0]


async def test_leader_scales_202(pool, dsn):
    c, _, _ = await hive(pool)
    async with client_for(create_app(make_settings(dsn), pool), IP0) as http:
        r = await http.post("/agent/v1/scale", json={"replicas": 4}, headers=headers(c["id"]))
    assert r.status_code == 202, r.text
    assert r.json() == {
        "session_id": str(c["id"]),
        "replicas": 4,
        "previous_replicas": 3,
        "max_replicas": 4,
    }
    assert await replicas_of(pool, c["id"]) == 4


async def test_non_leader_refused_403(pool, dsn):
    c, _, _ = await hive(pool)
    async with client_for(create_app(make_settings(dsn), pool), IP1) as http:
        r = await http.post("/agent/v1/scale", json={"replicas": 4}, headers=headers(c["id"]))
    assert_error(r, 403, "not_leader")
    assert await replicas_of(pool, c["id"]) == 3


async def test_leader_scales_below_own_ordinal_202(pool, dsn):
    c, a0, a1 = await hive(pool)
    async with pool.connection() as conn:
        await conn.execute("UPDATE agents SET wrapper_session_id = 'w0' WHERE id = %s", (a0["id"],))
        await conn.execute(
            "UPDATE session_leaders SET agent_id = %s WHERE session_id = %s", (a1["id"], c["id"])
        )
    async with client_for(create_app(make_settings(dsn), pool), IP1) as http:
        r = await http.post("/agent/v1/scale", json={"replicas": 1}, headers=headers(c["id"]))
    assert r.status_code == 202, r.text
    assert r.json() == {
        "session_id": str(c["id"]),
        "replicas": 1,
        "previous_replicas": 3,
        "max_replicas": 4,
    }
    assert await replicas_of(pool, c["id"]) == 1


async def test_agent_scale_refusals(pool, dsn):
    c, _, _ = await hive(pool)
    s, _, _ = await hive(pool, replicas=2, max_replicas=2, static=True)
    gone = await insert_session(pool, spec={"replicas": 2, "max_replicas": 3}, desired="deleting")
    g0 = await insert_agent(pool, gone["id"], ordinal=0, ready=True)
    await set_ip(pool, g0["id"], IP0)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO session_leaders (session_id, agent_id, epoch, since, reason)"
            " VALUES (%s, %s, 1, now(), 'start')",
            (gone["id"], g0["id"]),
        )
    async with client_for(create_app(make_settings(dsn), pool), IP0) as http:
        post = http.post
        assert_error(
            await post("/agent/v1/scale", json={"replicas": 1}, headers=headers(s["id"])),
            409,
            "static_session",
        )
        assert_error(
            await post("/agent/v1/scale", json={"replicas": 5}, headers=headers(c["id"])),
            422,
            "out_of_range",
        )
        assert_error(
            await post("/agent/v1/scale", json={"replicas": 0}, headers=headers(c["id"])),
            422,
            "out_of_range",
        )
        # the leader is ordinal 0 here, so make ordinal 1 lead to refuse shrinking to 1
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE session_leaders SET agent_id = (SELECT id FROM agents"
                " WHERE session_id = %s AND ordinal = 1) WHERE session_id = %s",
                (c["id"], c["id"]),
            )
        await set_ip(pool, (await _agent(pool, c["id"], 0))["id"], "10.9.0.99")
        await set_ip(pool, (await _agent(pool, c["id"], 1))["id"], IP0)
        # ordinal 0 stays at 1 replica but is not ready, so no agent that stays can take over
        async with pool.connection() as conn:
            await conn.execute(
                "UPDATE agents SET ready = false, wrapper_session_id = 'w0'"
                " WHERE session_id = %s AND ordinal = 0",
                (c["id"],),
            )
        assert_error(
            await post("/agent/v1/scale", json={"replicas": 1}, headers=headers(c["id"])),
            409,
            "leader_would_leave",
        )
        assert_error(
            await post("/agent/v1/scale", json={"replicas": 3}, headers=headers(gone["id"])),
            409,
            "session_deleting",
        )
        assert_error(
            await post(
                "/agent/v1/scale", json={"replicas": 3}, headers={"X-Orch-Session": str(c["id"])}
            ),
            401,
            "unauthorized",
        )
        assert_error(
            await post("/agent/v1/scale", json={"replicas": "x"}, headers=headers(c["id"])),
            422,
            "invalid_request",
        )
    async with client_for(create_app(make_settings(dsn), pool), "10.9.9.9") as http:
        r = await http.post("/agent/v1/scale", json={"replicas": 3}, headers=headers(c["id"]))
    assert_error(r, 403, "forbidden")


async def _agent(pool, session_id, ordinal):
    async with pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM agents WHERE session_id = %s AND ordinal = %s", (session_id, ordinal)
        )
        return {"id": (await cur.fetchone())[0]}


async def test_scale_ports_are_separate(pool, dsn):
    c, _, _ = await hive(pool)
    app = create_app(make_settings(dsn), pool)
    transport = httpx.ASGITransport(app=app, client=(IP0, 4321))
    async with httpx.AsyncClient(transport=transport, base_url="http://orch:8000") as pub:
        r = await pub.post("/agent/v1/scale", json={"replicas": 4}, headers=headers(c["id"]))
        assert r.status_code == 404, r.text
    async with client_for(app, IP0) as agent_port:
        r = await agent_port.post(f"/api/v1/sessions/{c['id']}/scale", json={"replicas": 4})
        assert r.status_code == 404, r.text
    assert await replicas_of(pool, c["id"]) == 3


async def test_plugin_scale_tool_contract(pool, dsn):
    c, _, _ = await hive(pool)
    app = create_app(make_settings(dsn), pool)

    def config(slug: str) -> dict:
        return {
            "base_url": f"http://orch:{AGENT_PORT}",
            "token": token(c["id"]),
            "session": str(c["id"]),
            "agent": slug,
            "slug": slug,
            "max_replicas": 4,
            "hive": True,
        }

    leader = httpx.ASGITransport(app=app, client=(IP0, 4321))
    async with PluginHarness(OrchestratorPlugin, config=config("a0"), transport=leader) as h:
        res = await h.call("scale", {"replicas": 4})
    assert not res.is_error, res.text
    assert json.loads(res.text) == {
        "session_id": str(c["id"]),
        "replicas": 4,
        "previous_replicas": 3,
        "max_replicas": 4,
    }
    assert await replicas_of(pool, c["id"]) == 4

    other = httpx.ASGITransport(app=app, client=(IP1, 4321))
    async with PluginHarness(OrchestratorPlugin, config=config("a1"), transport=other) as h:
        res = await h.call("scale", {"replicas": 2})
    assert res.is_error
    assert "not_leader" in res.text
    assert await replicas_of(pool, c["id"]) == 4
