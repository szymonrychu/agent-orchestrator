"""Autoclose sweeper: due clusters flip to deleting through the discard path."""

from __future__ import annotations

import asyncio
import json

import psycopg
from factories import insert_cluster

from agent_orchestrator.api import autoclose
from agent_orchestrator.api.app import create_app
from agent_orchestrator.db import clusters
from agent_orchestrator.settings import Settings


async def _backdate(pool, cluster_id, seconds: int) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE clusters SET created_at = now() - make_interval(secs => %s) WHERE id = %s",
            (seconds, cluster_id),
        )


async def _row(pool, cluster_id):
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT * FROM clusters WHERE id = %s", (cluster_id,))
        cols = [d.name for d in cur.description]
        return dict(zip(cols, await cur.fetchone(), strict=True))


async def test_close_expired_flips_only_due_clusters(pool, dsn):
    due = await insert_cluster(pool, spec={"autoclose_after_seconds": 300})
    young = await insert_cluster(pool, spec={"autoclose_after_seconds": 300})
    none = await insert_cluster(pool, spec={})
    gone = await insert_cluster(pool, spec={"autoclose_after_seconds": 300}, desired="deleting")
    for c in (due, young, none, gone):
        await _backdate(pool, c["id"], 301 if c is not young else 10)
    before = {c["id"]: await _row(pool, c["id"]) for c in (due, young, none, gone)}

    notified: list[dict] = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as lconn:
        await lconn.execute("LISTEN cluster_changed")
        async with pool.connection() as conn:
            closed = await clusters.close_expired(conn)
        async for n in lconn.notifies(timeout=2, stop_after=1):
            notified.append(json.loads(n.payload))

    assert closed == [due["id"]]
    assert notified == [{"cluster_id": str(due["id"])}]
    after = {c["id"]: await _row(pool, c["id"]) for c in (due, young, none, gone)}
    for cid in (young["id"], none["id"], gone["id"]):
        assert after[cid] == before[cid]
    d_before, d_after = before[due["id"]], after[due["id"]]
    assert (d_after["desired"], d_after["generation"]) == ("deleting", d_before["generation"] + 1)
    changed = {k for k in d_before if d_before[k] != d_after[k]}
    assert changed == {"desired", "generation"}


async def test_close_expired_is_idempotent(pool):
    ids = []
    for _ in range(5):
        c = await insert_cluster(pool, spec={"autoclose_after_seconds": 300})
        await _backdate(pool, c["id"], 400)
        ids.append(c["id"])

    async def one():
        async with pool.connection() as conn:
            return await clusters.close_expired(conn)

    a, b = await asyncio.gather(one(), one())
    assert sorted(a + b) == sorted(ids)
    for cid in ids:
        assert (await _row(pool, cid))["generation"] == 2


async def test_sweeper_runs_in_api_lifespan(dsn, pool, monkeypatch):
    monkeypatch.setattr(autoclose, "AUTOCLOSE_POLL_SECONDS", 0.1)
    due = await insert_cluster(pool, spec={"autoclose_after_seconds": 300})
    await _backdate(pool, due["id"], 400)
    settings = Settings(database_url=dsn, agent_image="img:1")
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        task = app.state.autoclose_task
        for _ in range(50):
            if (await _row(pool, due["id"]))["desired"] == "deleting":
                break
            await asyncio.sleep(0.1)
        assert (await _row(pool, due["id"]))["desired"] == "deleting"
        assert not task.done()
    assert task.cancelled() or task.done()
