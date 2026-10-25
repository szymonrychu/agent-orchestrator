"""events repository: idempotent insert, NOTIFY hints, bidirectional paging."""

from __future__ import annotations

import asyncio
import json
import uuid

import psycopg
import pytest
from factories import insert_agent, insert_cluster

from agents_orchestrator.db import events as ev


def env(seq: int, type: str = "message", turn_id: str | None = None, **data) -> dict:
    if turn_id is not None:
        data["turn_id"] = turn_id
    return {"seq": seq, "ts": "2026-01-01T00:00:00+00:00", "type": type, "data": data}


async def _setup(pool, agents: int = 1):
    cluster = await insert_cluster(pool)
    ags = [await insert_agent(pool, cluster["id"]) for _ in range(agents)]
    return cluster["id"], [a["id"] for a in ags]


async def _insert(pool, cid, aid, envelopes, sid="s1"):
    async with pool.connection() as conn:
        return await ev.insert_events(conn, cid, aid, sid, envelopes)


async def _page(pool, cid, **kw):
    async with pool.connection() as conn:
        return await ev.page(conn, cid, **kw)


async def test_insert_is_idempotent(pool):
    cid, (aid,) = await _setup(pool)
    envs = [env(1, turn_id="t1"), env(2), env(3)]
    first = await _insert(pool, cid, aid, envs)
    second = await _insert(pool, cid, aid, envs)
    assert first is not None and second is None
    p = await _page(pool, cid)
    assert [e["seq"] for e in p.events] == [1, 2, 3]
    assert p.events[0]["turn_id"] == "t1"
    assert first == p.events[-1]["id"]
    async with pool.connection() as conn:
        assert await ev.last_seq(conn, aid, "s1") == 3
        assert await ev.last_seq(conn, aid, "other") == 0


async def test_notify_only_on_new_rows(pool, dsn):
    cid, (aid,) = await _setup(pool)
    got: list[dict] = []
    async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as lc:
        await lc.execute("LISTEN events")
        first = await _insert(pool, cid, aid, [env(1), env(2)])
        await _insert(pool, cid, aid, [env(1), env(2)])

        async def drain():
            async for n in lc.notifies(timeout=1.0):
                got.append(json.loads(n.payload))

        await asyncio.wait_for(drain(), 5)
    assert got == [{"cluster_id": str(cid), "max_id": first}]


async def test_page_newest_then_before(pool):
    cid, (aid,) = await _setup(pool)
    await _insert(pool, cid, aid, [env(i) for i in range(1, 11)])
    p = await _page(pool, cid, limit=4)
    assert [e["seq"] for e in p.events] == [7, 8, 9, 10]
    assert p.has_more_before is True
    p2 = await _page(pool, cid, limit=4, before_id=p.events[0]["id"])
    assert [e["seq"] for e in p2.events] == [3, 4, 5, 6]
    assert p2.has_more_before is True
    p3 = await _page(pool, cid, limit=4, before_id=p2.events[0]["id"])
    assert [e["seq"] for e in p3.events] == [1, 2]
    assert p3.has_more_before is False


async def test_page_after_id(pool):
    cid, (aid,) = await _setup(pool)
    await _insert(pool, cid, aid, [env(i) for i in range(1, 8)])
    full = await _page(pool, cid, limit=100)
    anchor = full.events[2]["id"]
    p = await _page(pool, cid, after_id=anchor, limit=3)
    assert [e["seq"] for e in p.events] == [4, 5, 6]
    assert p.next_after_id == p.events[-1]["id"]
    empty = await _page(pool, cid, after_id=full.events[-1]["id"])
    assert empty.events == [] and empty.next_after_id == full.events[-1]["id"]


async def test_filter_agent_and_types(pool):
    cid, (a, b) = await _setup(pool, 2)
    await _insert(pool, cid, a, [env(1, "message"), env(2, "tool_use")])
    await _insert(pool, cid, b, [env(1, "thinking"), env(2, "message")])
    p = await _page(pool, cid, agent_id=a)
    assert [e["type"] for e in p.events] == ["message", "tool_use"]
    p = await _page(pool, cid, types=["message"])
    assert {e["agent_id"] for e in p.events} == {a, b} and len(p.events) == 2
    p = await _page(pool, cid, agent_id=b, types=["message", "tool_use"])
    assert [e["type"] for e in p.events] == ["message"]


async def test_merged_order_across_agents(pool):
    cid, (a, b) = await _setup(pool, 2)
    await _insert(pool, cid, a, [env(1)])
    await _insert(pool, cid, b, [env(1)])
    await _insert(pool, cid, a, [env(2)])
    p = await _page(pool, cid)
    assert [(e["agent_id"], e["seq"]) for e in p.events] == [(a, 1), (b, 1), (a, 2)]
    assert [e["id"] for e in p.events] == sorted(e["id"] for e in p.events)


async def test_gap_row(pool):
    cid, (aid,) = await _setup(pool)
    await _insert(pool, cid, aid, [env(1)])
    async with pool.connection() as conn:
        await ev.insert_gap(conn, cid, aid, "s1", 1, 9)
    p = await _page(pool, cid)
    gap = p.events[-1]
    assert gap["type"] == "gap"
    assert gap["data"] == {"after_seq": 1, "before_seq": 9}


async def test_before_and_after_rejected(pool):
    cid = uuid.uuid4()
    with pytest.raises(ValueError):
        await _page(pool, cid, before_id=5, after_id=2)


async def test_limit_capped(pool):
    cid, (aid,) = await _setup(pool)
    await _insert(pool, cid, aid, [env(i) for i in range(1, 503)])
    p = await _page(pool, cid, limit=10_000)
    assert len(p.events) == 500
