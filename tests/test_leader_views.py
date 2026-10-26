"""Leader, cap and context in the session and agent views, and the leader_changed frame."""

from __future__ import annotations

from factories import insert_agent, insert_session, set_agent_status
from test_api_sessions import make_settings
from test_api_streams import open_sse, serve, srv  # noqa: F401

from agents_orchestrator.api.app import create_app
from agents_orchestrator.db import leadership
from agents_orchestrator.db.pool import notify


async def _lead(pool, session_id, agent_id, epoch=3):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO session_leaders (session_id, agent_id, epoch, since, reason)"
            " VALUES (%s,%s,%s,now(),'test')"
            " ON CONFLICT (session_id) DO UPDATE SET agent_id = EXCLUDED.agent_id,"
            " epoch = EXCLUDED.epoch",
            (session_id, agent_id, epoch),
        )


async def test_session_view_leader_fields(pool, dsn):
    hive = await insert_session(pool, spec={"replicas": 2, "max_replicas": 4})
    alone = await insert_session(pool, spec={"replicas": 1, "static": True})
    ag = await insert_agent(pool, hive["id"], ready=True)
    await _lead(pool, hive["id"], ag["id"], epoch=5)
    app = create_app(make_settings(dsn, allowed_cidrs="127.0.0.0/8"), pool)
    async with serve(app) as s, s.client() as c:
        h = (await c.get(f"/api/v1/sessions/{hive['id']}")).json()
        a = (await c.get(f"/api/v1/sessions/{alone['id']}")).json()
        listed = {x["id"]: x for x in (await c.get("/api/v1/sessions")).json()}
    assert (h["hive"], h["max_replicas"], h["static"]) == (True, 4, False)
    assert (h["leader_agent_id"], h["leader_epoch"]) == (str(ag["id"]), 5)
    assert (a["hive"], a["max_replicas"], a["static"]) == (False, 1, True)
    assert (a["leader_agent_id"], a["leader_epoch"]) == (None, None)
    assert listed[str(hive["id"])]["leader_epoch"] == 5


async def test_agent_view_leader_and_context(pool, dsn):
    cl = await insert_session(pool, spec={"replicas": 2, "max_replicas": 2})
    lead = await insert_agent(pool, cl["id"], ready=True)
    other = await insert_agent(pool, cl["id"], ready=True)
    await set_agent_status(pool, lead["id"], context_pct=42.5)
    await set_agent_status(pool, other["id"])
    await _lead(pool, cl["id"], lead["id"])
    app = create_app(make_settings(dsn, allowed_cidrs="127.0.0.0/8"), pool)
    async with serve(app) as s, s.client() as c:
        rows = {x["id"]: x for x in (await c.get(f"/api/v1/sessions/{cl['id']}/agents")).json()}
    assert rows[str(lead["id"])]["leader"] is True
    assert rows[str(lead["id"])]["context_pct"] == 42.5
    assert rows[str(other["id"])]["leader"] is False
    assert rows[str(other["id"])]["context_pct"] is None


async def test_leader_changed_frame(srv, pool):  # noqa: F811
    cl = await insert_session(pool, spec={"replicas": 2, "max_replicas": 2})
    a1 = await insert_agent(pool, cl["id"], ready=True)
    a2 = await insert_agent(pool, cl["id"], ready=True)
    await set_agent_status(pool, a1["id"])
    await set_agent_status(pool, a2["id"])
    await _lead(pool, cl["id"], a1["id"], epoch=1)
    async with open_sse(srv, "/api/v1/stream") as sse:
        assert (await sse.next())["event"] == "snapshot"
        await _lead(pool, cl["id"], a2["id"], epoch=2)
        async with pool.connection() as conn:
            await notify(
                conn,
                "leader_changed",
                {
                    "session_id": str(cl["id"]),
                    "agent_id": str(a2["id"]),
                    "epoch": 2,
                    "reason": "test",
                },
            )
        seen: dict[str, list[dict]] = {}
        for _ in range(6):
            f = await sse.next()
            seen.setdefault(f["event"], []).append(f["data"])
            agents = {x["agent"]["id"] for x in seen.get("agent_status", [])}
            if {"leader_changed", "session_status"} <= set(seen) and str(a2["id"]) in agents:
                break
    assert seen["leader_changed"] == [
        {"session_id": str(cl["id"]), "agent_id": str(a2["id"]), "epoch": 2, "reason": "test"}
    ]
    assert seen["session_status"][0]["session"]["leader_epoch"] == 2
    assert seen["session_status"][0]["session"]["leader_agent_id"] == str(a2["id"])
    leaders = {x["agent"]["id"]: x["agent"]["leader"] for x in seen["agent_status"]}
    assert leaders[str(a2["id"])] is True


async def test_auto_handover_reason_in_frame_and_view(srv, pool, dsn):  # noqa: F811
    cl = await insert_session(pool, spec={"replicas": 2, "max_replicas": 3})
    agents = []
    for i in range(3):
        a = await insert_agent(pool, cl["id"], ordinal=i, ready=True, wrapper_session_id=f"s{i}")
        await set_agent_status(pool, a["id"], state="idle")
        agents.append(a)
    await _lead(pool, cl["id"], agents[2]["id"], epoch=1)  # ordinal 2 is above replicas 2
    async with open_sse(srv, "/api/v1/stream") as sse:
        assert (await sse.next())["event"] == "snapshot"
        async with pool.connection() as conn:
            change = await leadership.leadership_pass(conn, cl["id"])
        assert change is not None and change.reason == "scale_down"
        frames: list[dict] = []
        for _ in range(8):
            f = await sse.next()
            if f["event"] == "leader_changed":
                frames.append(f["data"])
                break
    assert frames == [
        {
            "session_id": str(cl["id"]),
            "agent_id": str(change.agent_id),
            "epoch": 2,
            "reason": "scale_down",
        }
    ]
    app = create_app(make_settings(dsn, allowed_cidrs="127.0.0.0/8"), pool)
    async with serve(app) as s, s.client() as c:
        view = (await c.get(f"/api/v1/sessions/{cl['id']}")).json()
    assert (view["leader_epoch"], view["leader_reason"]) == (2, "scale_down")
