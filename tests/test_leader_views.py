"""Leader, cap and context in the cluster and agent views, and the leader_changed frame."""

from __future__ import annotations

from factories import insert_agent, insert_cluster, set_agent_status
from test_api_clusters import make_settings
from test_api_streams import open_sse, serve, srv  # noqa: F401

from agent_orchestrator.api.app import create_app
from agent_orchestrator.db.pool import notify


async def _lead(pool, cluster_id, agent_id, epoch=3):
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO cluster_leaders (cluster_id, agent_id, epoch, since, reason)"
            " VALUES (%s,%s,%s,now(),'test')"
            " ON CONFLICT (cluster_id) DO UPDATE SET agent_id = EXCLUDED.agent_id,"
            " epoch = EXCLUDED.epoch",
            (cluster_id, agent_id, epoch),
        )


async def test_cluster_view_leader_fields(pool, dsn):
    hive = await insert_cluster(pool, spec={"replicas": 2, "max_replicas": 4})
    alone = await insert_cluster(pool, spec={"replicas": 1, "static": True})
    ag = await insert_agent(pool, hive["id"], ready=True)
    await _lead(pool, hive["id"], ag["id"], epoch=5)
    app = create_app(make_settings(dsn, allowed_cidrs="127.0.0.0/8"), pool)
    async with serve(app) as s, s.client() as c:
        h = (await c.get(f"/api/v1/clusters/{hive['id']}")).json()
        a = (await c.get(f"/api/v1/clusters/{alone['id']}")).json()
        listed = {x["id"]: x for x in (await c.get("/api/v1/clusters")).json()}
    assert (h["hive"], h["max_replicas"], h["static"]) == (True, 4, False)
    assert (h["leader_agent_id"], h["leader_epoch"]) == (str(ag["id"]), 5)
    assert (a["hive"], a["max_replicas"], a["static"]) == (False, 1, True)
    assert (a["leader_agent_id"], a["leader_epoch"]) == (None, None)
    assert listed[str(hive["id"])]["leader_epoch"] == 5


async def test_agent_view_leader_and_context(pool, dsn):
    cl = await insert_cluster(pool, spec={"replicas": 2, "max_replicas": 2})
    lead = await insert_agent(pool, cl["id"], ready=True)
    other = await insert_agent(pool, cl["id"], ready=True)
    await set_agent_status(pool, lead["id"], context_pct=42.5)
    await set_agent_status(pool, other["id"])
    await _lead(pool, cl["id"], lead["id"])
    app = create_app(make_settings(dsn, allowed_cidrs="127.0.0.0/8"), pool)
    async with serve(app) as s, s.client() as c:
        rows = {x["id"]: x for x in (await c.get(f"/api/v1/clusters/{cl['id']}/agents")).json()}
    assert rows[str(lead["id"])]["leader"] is True
    assert rows[str(lead["id"])]["context_pct"] == 42.5
    assert rows[str(other["id"])]["leader"] is False
    assert rows[str(other["id"])]["context_pct"] is None


async def test_leader_changed_frame(srv, pool):  # noqa: F811
    cl = await insert_cluster(pool, spec={"replicas": 2, "max_replicas": 2})
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
                {"cluster_id": str(cl["id"]), "agent_id": str(a2["id"]), "epoch": 2},
            )
        seen: dict[str, list[dict]] = {}
        for _ in range(6):
            f = await sse.next()
            seen.setdefault(f["event"], []).append(f["data"])
            agents = {x["agent"]["id"] for x in seen.get("agent_status", [])}
            if {"leader_changed", "cluster_status"} <= set(seen) and str(a2["id"]) in agents:
                break
    assert seen["leader_changed"] == [
        {"cluster_id": str(cl["id"]), "agent_id": str(a2["id"]), "epoch": 2}
    ]
    assert seen["cluster_status"][0]["cluster"]["leader_epoch"] == 2
    assert seen["cluster_status"][0]["cluster"]["leader_agent_id"] == str(a2["id"])
    leaders = {x["agent"]["id"]: x["agent"]["leader"] for x in seen["agent_status"]}
    assert leaders[str(a2["id"])] is True
