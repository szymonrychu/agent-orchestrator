"""Hive state and feed push: the dispatcher's `push_once` against Postgres and the fake wrapper."""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import psycopg
import pytest
from factories import (
    insert_agent,
    insert_event,
    insert_message,
    insert_session,
    set_agent_status,
)
from fakes import fake_wrapper
from fakes.fake_wrapper import FakeWrapper
from psycopg.rows import dict_row

from agents_orchestrator.db import leadership, queue
from agents_orchestrator.db.queue import _CLAIM_LOCK_NAMESPACE
from agents_orchestrator.dispatcher import Dispatcher, hive
from agents_orchestrator.settings import Settings
from agents_orchestrator.tokens import tokens_for
from agents_orchestrator.wrapper.client import WrapperClient

TOKEN = "test-token"
KEY = "k" * 32
OLD = "now() - interval '60 seconds'"


def make_settings(dsn: str) -> Settings:
    return Settings(database_url=dsn, master_key=KEY)


@pytest.fixture
async def fake(monkeypatch):
    monkeypatch.setattr(fake_wrapper, "MAX_SESSIONS", 100)  # one fake hosts every agent session
    f = FakeWrapper(token=TOKEN)
    await f.start()
    try:
        yield f
    finally:
        await f.stop()


class Env:
    def __init__(self, pool, dsn: str, fake: FakeWrapper) -> None:
        self.pool, self.dsn, self.fake = pool, dsn, fake
        self.settings = make_settings(dsn)
        self.state = hive.HiveState()
        self.clock = 1000.0
        self.http_clients: list[WrapperClient] = []

    def factory(self, session_id, url) -> WrapperClient:
        client = WrapperClient(url, TOKEN)
        self.http_clients.append(client)
        return client

    async def push(self, state=None, advance: float = 100.0) -> int:
        """One push_once on a clock that moves `advance` seconds first (past the throttle)."""
        self.clock += advance
        return await hive.push_once(
            self.pool,
            self.settings,
            self.factory,
            state or self.state,
            clock=lambda: self.clock,
        )

    async def session(self, name: str = "s") -> str:
        client = WrapperClient(self.fake.base_url, TOKEN)
        try:
            return (
                await client.create_session(f"{name}{uuid.uuid4().hex[:4]}", "/w")
            ).wrapper_session_id
        finally:
            await client.aclose()

    async def hive(self, n: int = 3, *, spec: dict | None = None, lead: bool = True) -> tuple:
        spec = {"replicas": n, "max_replicas": 4, **(spec or {})}
        c = await insert_session(self.pool, spec=spec)
        agents = []
        for i in range(n):
            agents.append(await self.agent(c["id"], i))
        if lead:
            await self.lpass(c["id"])
        return c, agents

    async def agent(self, session_id, ordinal: int, *, ctx: float | None = None) -> dict:
        sid = await self.session()
        a = await insert_agent(
            self.pool,
            session_id,
            ordinal=ordinal,
            ready=True,
            wrapper_session_id=sid,
            url=self.fake.base_url,
        )
        await set_agent_status(self.pool, a["id"], state="idle", context_pct=ctx)
        return a

    async def lpass(self, session_id):
        async with self.pool.connection() as conn:
            return await leadership.leadership_pass(conn, session_id)

    async def run(self, sql: str, params: tuple = ()) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql, params)

    async def rows(self, sql: str, params: tuple = ()) -> list[dict]:
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return await cur.fetchall()

    async def leader_row(self, session_id) -> dict:
        return (
            await self.rows("SELECT * FROM session_leaders WHERE session_id = %s", (session_id,))
        )[0]

    def notes(self, agent: dict, key: str | None = "hive_state") -> list[str]:
        """Texts pushed to the agent's session: the keyed hive_state ones, or the appended."""
        pushed = self.fake.notes.get(agent["wrapper_session_id"], [])
        return [n["text"] for n in pushed if n["key"] == key]

    async def write_event(
        self, session_id, agent, path: str, *, age: float = 60.0, ts_age: float | None = None
    ) -> dict:
        """A Write call stored `age` s ago; its wrapper ts defaults to the same moment."""
        data = {"tool_use_id": uuid.uuid4().hex, "tool_name": "Write", "tool_input": {}}
        data["tool_input"] = {"file_path": path}
        return await insert_event(
            self.pool,
            session_id,
            agent["id"],
            type="tool_call",
            data=data,
            ts=await self.ago(age if ts_age is None else ts_age),
            created_at=await self.ago(age),
        )

    async def ago(self, seconds: float):
        return (await self.rows("SELECT now() - make_interval(secs => %s) AS t", (seconds,)))[0][
            "t"
        ]


@pytest.fixture
async def env(pool, dsn, fake):
    e = Env(pool, dsn, fake)
    try:
        yield e
    finally:
        for client in e.http_clients:
            await client.aclose()


# ---------------------------------------------------------------- hive_state


async def test_hive_state_on_leader_change(env):
    c, (a0, a1, a2) = await env.hive()
    assert await env.push() == 3
    for agent in (a0, a1, a2):
        (text,) = env.notes(agent)
        assert text.startswith("<hive-state>") and text.rstrip().endswith("</hive-state>")
        assert "in hive" in text and "of up to 4 agents" in text
        assert "Leader: agent-0 (epoch 1)" in text
    assert "You are agent-0 (leader)" in env.notes(a0)[0]
    assert "You are agent-2 (member)" in env.notes(a2)[0]
    assert all(f"agent-{i}" in env.notes(a1)[0] for i in range(3))
    sent = env.fake.notes[a0["wrapper_session_id"]][0]
    assert sent["source"] == "orchestrator" and sent["key"] == "hive_state"
    row = await env.leader_row(c["id"])
    assert row["state_pushed_at"] is not None
    assert set(json.loads(row["state_hash"])) == {str(a["id"]) for a in (a0, a1, a2)}

    # a user override moves the lead: every live agent gets the new state
    async with env.pool.connection() as conn:
        await leadership.insert_request(conn, c["id"], "user", None, a1["id"], "test", None)
    await env.lpass(c["id"])
    assert await env.push() == 3
    for agent in (a0, a1, a2):
        assert len(env.notes(agent)) == 2
        assert "Leader: agent-1 (epoch 2)" in env.notes(agent)[-1]
    assert "You are agent-1 (leader)" in env.notes(a1)[-1]


async def test_hive_state_on_membership_change_only(env):
    c, (a0, a1, a2) = await env.hive()
    await env.push()
    assert await env.push() == 0  # nothing changed
    assert all(len(env.notes(a)) == 1 for a in (a0, a1, a2))

    a3 = await env.agent(c["id"], 3)  # joins
    assert await env.push() == 4
    assert "agent-3" in env.notes(a0)[-1] and len(env.notes(a3)) == 1

    await env.run("UPDATE agents SET gone_at = now() WHERE id = %s", (a2["id"],))  # leaves
    assert await env.push() == 3
    assert "agent-2" not in env.notes(a0)[-1]
    assert len(env.notes(a2)) == 2  # a gone agent gets nothing more
    assert await env.push() == 0

    # context moves inside one 10% bucket: no push; across buckets: push
    await set_agent_status(env.pool, a1["id"], state="idle", context_pct=41)
    assert await env.push() == 3
    await set_agent_status(env.pool, a1["id"], state="idle", context_pct=48)
    assert await env.push() == 0
    await set_agent_status(env.pool, a1["id"], state="busy", context_pct=48)
    assert await env.push() == 3
    assert "agent-1 (member, ready, busy, 48% context)" in env.notes(a0)[-1]


async def test_hive_state_threshold_hint(env):
    _, (a0, _a1, _a2) = await env.hive(spec={"auto_compact_pct": 80})
    await env.push()
    assert "Hand over leadership instead of compacting above 65% context" in env.notes(a0)[0]
    _, (b0, _b1, _b2) = await env.hive()
    await env.push()
    assert "Hand over leadership" not in env.notes(b0)[0]


async def test_hive_state_repush_on_session_change(env):
    c, (a0, a1, a2) = await env.hive()
    await env.push()
    new_session = await env.session()  # a fast wrapper restart: same pod, new session
    await env.run(
        "UPDATE agents SET wrapper_session_id = %s WHERE id = %s", (new_session, a1["id"])
    )
    assert await env.push() == 3
    assert len(env.fake.notes[new_session]) == 1
    assert len(env.notes(a0)) == 2


async def test_failed_agent_does_not_repush_others(env):
    c, (a0, a1, a2) = await env.hive()
    await env.run("UPDATE agents SET wrapper_session_id = 'ses_missing' WHERE id = %s", (a2["id"],))
    assert await env.push() == 2
    row = await env.leader_row(c["id"])
    assert row["state_pushed_at"] is None  # not every push succeeded
    assert set(json.loads(row["state_hash"])) == {str(a0["id"]), str(a1["id"])}
    before = len(env.fake.requests)
    assert await env.push() == 0  # a0 and a1 are not pushed again
    posts = [p for m, p in env.fake.requests[before:] if m == "POST"]
    assert posts == ["/v1/sessions/ses_missing/notes"]  # only the failed one is retried
    assert len(env.notes(a0)) == len(env.notes(a1)) == 1


# ---------------------------------------------------------------- feed


async def test_feed_push_and_cursor(env):
    c, (a0, a1, a2) = await env.hive()
    w1 = await env.write_event(c["id"], a1, "/work/a.py")
    bash = {"tool_use_id": "b1", "tool_name": "Bash", "tool_input": {"command": "make test"}}
    await insert_event(
        env.pool,
        c["id"],
        a2["id"],
        type="tool_call",
        data=bash,
        ts=await env.ago(50),
        created_at=await env.ago(50),
    )
    res = await insert_event(
        env.pool,
        c["id"],
        a2["id"],
        type="tool_result",
        data={"tool_use_id": "b1", "tool_name": "Bash", "exit_code": 2, "is_error": True},
        ts=await env.ago(49),
        created_at=await env.ago(49),
    )
    await env.push()
    for me, others in ((a0, ("agent-1 wrote /work/a.py", "agent-2 ran: make test (exit 2)")),):
        (feed,) = env.notes(me, key=None)
        assert all(line in feed for line in others)
    (own1,) = env.notes(a1, key=None)
    assert "agent-1 wrote" not in own1 and "agent-2 ran: make test (exit 2)" in own1
    (own2,) = env.notes(a2, key=None)
    assert "agent-2 ran" not in own2 and "agent-1 wrote /work/a.py" in own2
    assert env.fake.notes[a0["wrapper_session_id"]][-1]["key"] is None
    row = await env.leader_row(c["id"])
    assert row["feed_cursor"] == res["id"] > w1["id"]

    assert await env.push() == 0  # nothing new, nothing pushed
    w2 = await env.write_event(c["id"], a2, "/work/b.py")
    assert await env.push() == 2  # a0 and a1; a2 is the author
    assert "agent-2 wrote /work/b.py" in env.notes(a0, key=None)[-1]
    assert len(env.notes(a2, key=None)) == 1
    assert (await env.leader_row(c["id"]))["feed_cursor"] == w2["id"]


async def test_feed_cursor_starts_at_max(env):
    c = await insert_session(env.pool, spec={"replicas": 2, "max_replicas": 4})
    a0, a1 = await env.agent(c["id"], 0), await env.agent(c["id"], 1)
    old = await env.write_event(c["id"], a1, "/old/history.py", age=3600)
    await env.lpass(c["id"])  # the leader row is created here
    assert (await env.leader_row(c["id"]))["feed_cursor"] == old["id"]
    await env.push()
    assert env.notes(a0, key=None) == []  # history is not current activity
    await env.write_event(c["id"], a1, "/new/work.py")
    await env.push()
    (feed,) = env.notes(a0, key=None)
    assert "agent-1 wrote /new/work.py" in feed and "history" not in feed


async def test_feed_cursor_settle_window(env):
    c, (a0, a1, a2) = await env.hive()
    # a lower id that is still uncommitted while a higher id commits
    slow = await psycopg.AsyncConnection.connect(env.dsn)
    try:
        data = json.dumps(
            {"tool_use_id": "late", "tool_name": "Write", "tool_input": {"file_path": "/late.py"}}
        )
        cur = await slow.execute(
            "INSERT INTO events (session_id, agent_id, wrapper_session_id, seq, ts, type, data)"
            " VALUES (%s, %s, 's', 1, now() - interval '1 hour', 'tool_call', %s::jsonb)"
            " RETURNING id",
            (c["id"], a1["id"], data),
        )
        low_id = (await cur.fetchone())[0]
        # the wrapper stamped both rows long ago (a catch-up replay): only the insert time counts
        high = await env.write_event(c["id"], a2, "/high.py", age=0, ts_age=3600)
        assert high["id"] > low_id
        await env.push()
        assert env.notes(a0, key=None) == []  # inside the settle window: not consumed
        assert (await env.leader_row(c["id"]))["feed_cursor"] < low_id
        await slow.commit()
    finally:
        await slow.close()
    await env.run(
        "UPDATE events SET created_at = now() - interval '60 seconds' WHERE id >= %s", (low_id,)
    )
    await env.push()
    (feed,) = env.notes(a0, key=None)
    assert "agent-1 wrote /late.py" in feed and "agent-2 wrote /high.py" in feed
    assert (await env.leader_row(c["id"]))["feed_cursor"] == high["id"]


async def test_feed_pending_bash_carried(env):
    c, (a0, a1, a2) = await env.hive()
    call = {"tool_use_id": "slow", "tool_name": "Bash", "tool_input": {"command": "sleep 30"}}
    pending = await insert_event(
        env.pool,
        c["id"],
        a1["id"],
        type="tool_call",
        data=call,
        ts=await env.ago(10),
        created_at=await env.ago(10),
    )
    await env.write_event(c["id"], a2, "/other.py", age=9)
    await env.push()
    (feed,) = env.notes(a0, key=None)
    assert "agent-2 wrote /other.py" in feed and "sleep 30" not in feed
    # the cursor stays below the pending call
    assert (await env.leader_row(c["id"]))["feed_cursor"] == pending["id"] - 1

    await insert_event(
        env.pool,
        c["id"],
        a1["id"],
        type="tool_result",
        data={"tool_use_id": "slow", "tool_name": "Bash", "exit_code": 0},
        ts=await env.ago(8),
        created_at=await env.ago(8),
    )
    last = (await env.rows("SELECT max(id) AS m FROM events"))[0]["m"]
    await env.push()
    pushed = env.notes(a0, key=None)
    assert len(pushed) == 2 and "agent-1 ran: sleep 30 (ok)" in pushed[1]
    assert "other.py" not in pushed[1]  # not pushed twice
    assert (await env.leader_row(c["id"]))["feed_cursor"] == last


# ---------------------------------------------------------------- gating


async def test_throttle_and_lock(env):
    c, (a0, a1, a2) = await env.hive()
    state = hive.HiveState()
    now = [1000.0]

    async def tick() -> int:
        return await hive.push_once(
            env.pool, env.settings, env.factory, state, clock=lambda: now[0]
        )

    assert await tick() == 3
    await env.agent(c["id"], 3)
    now[0] += hive.PUSH_INTERVAL_SECONDS - 1
    assert await tick() == 0  # throttled: one push per session per interval
    now[0] += 2
    assert await tick() == 4

    # another dispatcher holds the session's lock: skipped, and the skip is not a tick
    await env.agent(c["id"], 4)
    now[0] += 100
    holder = await psycopg.AsyncConnection.connect(env.dsn)
    try:
        await holder.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s::text))", (_CLAIM_LOCK_NAMESPACE, c["id"])
        )
        assert await tick() == 0
        await holder.rollback()
    finally:
        await holder.close()
    assert await tick() == 5


async def test_alone_and_disabled(env):
    solo = await insert_session(env.pool, spec={"replicas": 1})
    s0 = await env.agent(solo["id"], 0)
    await env.write_event(solo["id"], s0, "/solo.py")
    assert await env.push() == 0
    assert env.fake.notes.get(s0["wrapper_session_id"]) is None
    assert (
        await env.rows("SELECT 1 FROM session_leaders WHERE session_id = %s", (solo["id"],)) == []
    )

    c, (a0, a1, a2) = await env.hive()
    await env.write_event(c["id"], a1, "/x.py")
    env.fake.disable_notes()
    assert await env.push() == 0  # 409 notes_disabled everywhere: tolerated, nothing delivered
    assert env.fake.notes == {}
    row = await env.leader_row(c["id"])
    assert row["state_pushed_at"] is not None  # nothing to retry on a wrapper without notes
    env.fake.disable_notes(False)
    assert await env.push() == 0  # and not retried every tick
    assert env.fake.notes == {}


async def test_hanging_wrapper_does_not_block_claim(env, monkeypatch):
    monkeypatch.setattr(hive, "PUSH_TIMEOUT_SECONDS", 1.5)
    c, (a0, a1, a2) = await env.hive()
    msg = await insert_message(env.pool, c["id"])
    env.fake.delay_notes(30)
    task = asyncio.create_task(env.push())
    await asyncio.sleep(0.5)  # the pushes are in flight, no transaction held
    assert not task.done()
    t0 = time.monotonic()
    async with env.pool.connection() as conn:
        claim = await queue.claim_and_pick(conn)
    await env.run("UPDATE agents SET gone_at = now() WHERE id = %s", (a0["id"],))
    change = await env.lpass(c["id"])
    elapsed = time.monotonic() - t0
    assert claim is not None and claim.message_id == msg["id"]
    assert change is not None and change.reason == "leader_lost"
    assert elapsed < 1.0 and not task.done()
    assert await task == 0  # the per-push timeout ended the hang
    row = await env.leader_row(c["id"])
    assert row["state_pushed_at"] is None  # failed pushes are not recorded


async def test_dispatcher_run_pushes_on_leader_changed(env):
    c, agents = await env.hive(lead=False)
    env.fake.token = tokens_for(env.settings, c["id"]).auth  # what the dispatcher presents
    stop = asyncio.Event()
    dispatcher = Dispatcher(env.pool, env.settings)
    task = asyncio.create_task(dispatcher.run(stop))
    try:
        async with asyncio.timeout(15):
            while not env.notes(agents[0]):
                await asyncio.sleep(0.1)
    finally:
        stop.set()
        await task
    assert "Leader: agent-0 (epoch 1)" in env.notes(agents[2])[0]


async def test_concurrent_pushes_claim_once(env):
    c, (a0, a1, a2) = await env.hive()
    await env.write_event(c["id"], a1, "/once.py")
    env.fake.delay_notes(0.5)  # the second dispatcher reads during the first one's HTTP phase
    first, second = hive.HiveState(), hive.HiveState()
    task = asyncio.create_task(env.push(first))
    await asyncio.sleep(0.25)  # the first is now in its HTTP phase, lock released
    sent = [await env.push(second), await task]
    assert sorted(sent) == [0, 5]  # 3 hive_state notes and 2 feed notes, once
    for agent in (a0, a1, a2):
        assert len(env.notes(agent)) == 1
    assert len(env.notes(a0, key=None)) == 1 and len(env.notes(a2, key=None)) == 1
