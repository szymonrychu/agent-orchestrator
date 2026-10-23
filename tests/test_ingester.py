"""Ingester (D5): one SSE subscription per agent session, stored events and live status."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
import pytest_asyncio
from factories import insert_agent, insert_cluster, insert_event, set_agent_status
from fakes.fake_wrapper import FakeWrapper
from psycopg import sql

from agent_orchestrator.db import status
from agent_orchestrator.ingester import Ingester
from agent_orchestrator.ingester.loop import map_state
from agent_orchestrator.settings import Settings
from agent_orchestrator.tokens import derive_tokens
from agent_orchestrator.wrapper import WrapperClient

MASTER_KEY = "k" * 40
OLD_SINCE = "2001-02-03T04:05:06+00:00"


# --------------------------------------------------------------------------- helpers


async def wait_until(pred: Callable[[], Any], timeout: float = 8.0, interval: float = 0.02):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = pred()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(interval)


async def fetch(pool, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        cur = await conn.execute(query, params)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]


async def status_row(pool, agent_id: uuid.UUID) -> dict[str, Any] | None:
    rows = await fetch(pool, "SELECT * FROM agent_status WHERE agent_id=%s", (agent_id,))
    return rows[0] if rows else None


async def events_rows(pool, agent_id: uuid.UUID) -> list[dict[str, Any]]:
    return await fetch(pool, "SELECT * FROM events WHERE agent_id=%s ORDER BY id", (agent_id,))


async def db_now(pool) -> datetime:
    return (await fetch(pool, "SELECT now() AS n"))[0]["n"]


class Listener:
    """Collects NOTIFY payloads on a dedicated autocommit connection."""

    def __init__(self) -> None:
        self.got: list[tuple[str, dict[str, Any]]] = []

    def on(self, channel: str) -> list[dict[str, Any]]:
        return [p for c, p in self.got if c == channel]


@pytest_asyncio.fixture
async def listener(dsn: str) -> AsyncIterator[Listener]:
    lst = Listener()
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    for ch in ("events", "status", "agent_free"):
        await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(ch)))

    async def pump() -> None:
        async for n in conn.notifies():
            lst.got.append((n.channel, json.loads(n.payload)))

    task = asyncio.create_task(pump())
    try:
        yield lst
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await conn.close()


class RecordingClient(WrapperClient):
    """A real client that records the Last-Event-ID of every stream it opens."""

    def __init__(self, *a: Any, opened: list[Any], replay_all: bool = False, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self._opened = opened
        self._replay_all = replay_all

    async def events(self, session_id: str, last_event_id: int | str | None = None):
        if self._replay_all:
            last_event_id = 0
        self._opened.append(last_event_id)
        async for env in super().events(session_id, last_event_id):
            yield env


class Harness:
    """A cluster, a fake wrapper with one session, an agent row and a running Ingester."""

    def __init__(self, pool, dsn: str) -> None:
        self.pool, self.dsn = pool, dsn
        self.opened: list[Any] = []
        self.sleeps: list[float] = []
        self.replay_all = False
        self.ingester: Ingester | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop: asyncio.Event | None = None

    async def setup(self, peers: dict[str, Any] | None = None) -> Harness:
        self.cluster = await insert_cluster(self.pool)
        self.cluster_id = self.cluster["id"]
        token = derive_tokens(MASTER_KEY.encode(), self.cluster_id).auth
        self.fake = FakeWrapper(token=token)
        if peers is not None:
            self.fake.peers = peers
        await self.fake.start()
        self.client = WrapperClient(self.fake.base_url, token)
        snap = await self.client.create_session("a0", "w")
        self.session_id = snap.session_id
        self.agent = await insert_agent(
            self.pool,
            self.cluster_id,
            url=self.fake.base_url,
            session_id=self.session_id,
            ready=True,
        )
        self.agent_id = self.agent["id"]
        return self

    def settings(self) -> Settings:
        return Settings(database_url=self.dsn, master_key=MASTER_KEY, ingest_refresh_seconds=0.05)

    def factory(self, url: str, cluster_id: uuid.UUID) -> WrapperClient:
        token = derive_tokens(MASTER_KEY.encode(), cluster_id).auth
        return RecordingClient(url, token, opened=self.opened, replay_all=self.replay_all)

    async def fast_sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        await asyncio.sleep(0.02)

    async def start(self, *, factory: bool = True) -> Ingester:
        self.ingester = Ingester(
            self.pool,
            self.settings(),
            self.factory if factory else None,
            sleep=self.fast_sleep,
        )
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.ingester.run(self._stop))
        return self.ingester

    async def stop(self) -> None:
        if self._task is not None and self._stop is not None:
            self._stop.set()
            await asyncio.wait_for(self._task, 10)
            self._task = None

    async def close(self) -> None:
        await self.stop()
        await self.client.aclose()
        await self.fake.stop()

    async def stored_through(self, seq: int) -> None:
        async def done() -> bool:
            row = await status_row(self.pool, self.agent_id)
            return row is not None and (row["last_seq"] or 0) >= seq

        await wait_until(done)

    def last_fake_seq(self) -> int:
        return self.fake.sessions[self.session_id].events[-1]["seq"]


@pytest_asyncio.fixture
async def harness(pool, dsn: str) -> AsyncIterator[Callable[..., Any]]:
    made: list[Harness] = []

    async def make(peers: dict[str, Any] | None = None) -> Harness:
        h = await Harness(pool, dsn).setup(peers)
        made.append(h)
        return h

    try:
        yield make
    finally:
        for h in made:
            await h.close()


# --------------------------------------------------------------------------- tests


def test_state_mapping() -> None:
    assert map_state("ready") == "idle"
    assert map_state("busy") == "busy"
    assert map_state("creating") == "booting"
    for s in ("stopped", "failed", "dead"):
        assert map_state(s) == "dead"
    assert map_state("something-new") == "unknown"


async def test_attach_seeds_status_from_snapshot(harness, pool) -> None:
    h = await harness()
    h.fake.set_context(used_tokens=50_000, window_tokens=200_000)
    idle_since = h.fake.sessions[h.session_id].idle_since
    before = await db_now(pool)
    await h.start()
    await h.stored_through(h.last_fake_seq())

    row = await status_row(pool, h.agent_id)
    assert row["state"] == "idle"
    assert row["wrapper_idle_since"] == datetime.fromisoformat(idle_since)
    assert row["free_since"] is not None and row["free_since"] >= before
    assert row["context_used_tokens"] == 50_000
    assert row["context_window_tokens"] == 200_000
    assert row["context_pct"] == pytest.approx(25.0)
    assert h.opened[0] == 0  # a fresh session streams from the start

    # a null snapshot context clears the columns on the next attach
    await h.stop()
    h.fake.set_context()
    await h.start()
    await wait_until(lambda: len(h.opened) >= 2)

    async def cleared() -> bool:
        r = await status_row(pool, h.agent_id)
        return r["context_used_tokens"] is None

    await wait_until(cleared)
    row = await status_row(pool, h.agent_id)
    assert row["context_window_tokens"] is None and row["context_pct"] is None


async def test_attach_maps_creating_to_booting(harness, pool) -> None:
    h = await harness()
    # the log is already stored, so the stream replays nothing and the snapshot decides
    for env in h.fake.sessions[h.session_id].events:
        await insert_event(
            pool,
            h.cluster_id,
            h.agent_id,
            session_id=h.session_id,
            seq=env["seq"],
            type=env["type"],
            data=env["data"],
        )
    await set_agent_status(pool, h.agent_id, state="idle", free_since=None)
    h.fake.sessions[h.session_id].state = "creating"
    await h.start()

    async def booting() -> bool:
        r = await status_row(pool, h.agent_id)
        return r["state"] == "booting"

    await wait_until(booting)
    row = await status_row(pool, h.agent_id)
    assert row["free_since"] is None
    assert row["wrapper_idle_since"] is None
    assert row["last_seq"] == h.last_fake_seq()
    await wait_until(lambda: h.opened)
    assert h.opened == [h.last_fake_seq()]


async def test_events_stored_and_notified(harness, pool, listener) -> None:
    h = await harness()
    await h.start()
    await h.stored_through(h.last_fake_seq())
    h.fake.emit("assistant_text", {"text": "hello there"})
    await h.stored_through(h.last_fake_seq())

    rows = await events_rows(pool, h.agent_id)
    fake_events = h.fake.sessions[h.session_id].events
    assert [r["seq"] for r in rows] == [e["seq"] for e in fake_events]
    assert [r["type"] for r in rows] == [e["type"] for e in fake_events]
    assert rows[-1]["data"] == {"text": "hello there"}
    assert all(r["cluster_id"] == h.cluster_id and r["session_id"] == h.session_id for r in rows)

    await wait_until(lambda: rows[-1]["id"] in [p["max_id"] for p in listener.on("events")])
    assert all(p["cluster_id"] == str(h.cluster_id) for p in listener.on("events"))


async def test_resume_uses_last_event_id_no_duplicates(harness, pool) -> None:
    h = await harness()
    await h.start()
    h.fake.emit("assistant_text", {"text": "one"})
    first_seq = h.last_fake_seq()
    await h.stored_through(first_seq)

    h.fake.drop_streams()
    await wait_until(lambda: len(h.opened) >= 2)
    h.fake.emit("assistant_text", {"text": "two"})
    await h.stored_through(h.last_fake_seq())

    assert h.opened[0] == 0
    assert h.opened[1] == first_seq

    # a fresh ingester process resumes from the stored last_seq too
    await h.stop()
    h.fake.emit("assistant_text", {"text": "three"})
    await h.start()
    await h.stored_through(h.last_fake_seq())
    assert h.opened[-1] == first_seq + 1

    rows = await events_rows(pool, h.agent_id)
    seqs = [r["seq"] for r in rows]
    assert seqs == sorted(set(seqs))
    assert seqs == [e["seq"] for e in h.fake.sessions[h.session_id].events]


async def test_gap_row_on_seq_hole(harness, pool) -> None:
    h = await harness()
    await h.start()
    before = h.last_fake_seq()
    await h.stored_through(before)

    h.fake.skip_seq(3)
    h.fake.emit("assistant_text", {"text": "after the hole"})
    after = h.last_fake_seq()
    await h.stored_through(after)

    gaps = [r for r in await events_rows(pool, h.agent_id) if r["type"] == "gap"]
    assert len(gaps) == 1
    assert gaps[0]["data"] == {"after_seq": before, "before_seq": after}
    rows = await events_rows(pool, h.agent_id)
    # the gap row is written before the event that revealed it
    assert [r["type"] for r in rows][-2:] == ["gap", "assistant_text"]


async def test_idle_sets_free_since_and_notifies_agent_free(harness, pool, listener) -> None:
    h = await harness()
    await h.start()
    await h.stored_through(h.last_fake_seq())

    h.fake.emit("session_state", {"state": "busy", "since": OLD_SINCE})
    await h.stored_through(h.last_fake_seq())
    busy = await status_row(pool, h.agent_id)
    assert busy["state"] == "busy"
    assert busy["wrapper_idle_since"] is None
    listener.got.clear()

    before = await db_now(pool)
    h.fake.emit("session_state", {"state": "ready", "since": OLD_SINCE})
    await h.stored_through(h.last_fake_seq())
    after = await db_now(pool)

    row = await status_row(pool, h.agent_id)
    assert row["state"] == "idle"
    assert before <= row["free_since"] <= after  # the database clock
    assert row["free_since"] != datetime.fromisoformat(OLD_SINCE)
    assert row["wrapper_idle_since"] == datetime.fromisoformat(OLD_SINCE)

    want = {"cluster_id": str(h.cluster_id), "agent_id": str(h.agent_id)}
    await wait_until(lambda: listener.on("agent_free"))
    assert listener.on("agent_free") == [want]
    await wait_until(lambda: listener.on("status"))
    assert all(p["cluster_id"] == str(h.cluster_id) for p in listener.on("status"))


async def test_repeated_idle_does_not_move_free_since(harness, pool, listener) -> None:
    h = await harness()
    await h.start()
    await h.stored_through(h.last_fake_seq())
    first = await status_row(pool, h.agent_id)
    assert first["state"] == "idle"
    listener.got.clear()

    # a repeated idle event (e.g. a restart notice) while already idle
    h.fake.emit("session_state", {"state": "ready", "since": OLD_SINCE})
    await h.stored_through(h.last_fake_seq())
    row = await status_row(pool, h.agent_id)
    assert row["free_since"] == first["free_since"]
    assert row["wrapper_idle_since"] == first["wrapper_idle_since"]

    # a replay of the whole log (a fresh process whose stream starts at seq 0)
    await h.stop()
    h.replay_all = True
    await h.start()
    await wait_until(lambda: len(h.opened) >= 2)
    h.fake.emit("assistant_text", {"text": "marker"})
    await h.stored_through(h.last_fake_seq())
    row = await status_row(pool, h.agent_id)
    assert row["free_since"] == first["free_since"]
    assert listener.on("agent_free") == []

    # and the repository rule on its own
    async with pool.connection() as conn:
        change = await status.set_state(conn, h.agent_id, "idle", since=OLD_SINCE)
    assert not change.became_idle
    assert (await status_row(pool, h.agent_id))["free_since"] == first["free_since"]


async def test_context_event_updates_status(harness, pool, listener) -> None:
    h = await harness()
    await h.start()
    await h.stored_through(h.last_fake_seq())
    listener.got.clear()

    ctx = {
        "used_tokens": 120_000,
        "window_tokens": 200_000,
        "used_percentage": 60.0,
        "model": "claude-sonnet-4-5",
        "source": "statusline",
        "updated_at": datetime.now(UTC).isoformat(),
    }
    h.fake.emit("context", ctx)
    await h.stored_through(h.last_fake_seq())
    row = await status_row(pool, h.agent_id)
    assert (row["context_used_tokens"], row["context_window_tokens"]) == (120_000, 200_000)
    assert row["context_pct"] == pytest.approx(60.0)
    await wait_until(lambda: listener.on("status"))

    # compaction resets the gauge: the wrapper sends nulls
    h.fake.emit("context", {**dict.fromkeys(("used_tokens", "window_tokens", "used_percentage"))})
    await h.stored_through(h.last_fake_seq())
    row = await status_row(pool, h.agent_id)
    assert row["context_used_tokens"] is None
    assert row["context_window_tokens"] is None
    assert row["context_pct"] is None


async def test_turn_projection(harness, pool) -> None:
    h = await harness()
    await h.start()
    await h.stored_through(h.last_fake_seq())

    accepted = await h.client.submit_turn(h.session_id, "do the thing")
    turn_id = accepted.turn_id
    await h.stored_through(h.last_fake_seq())
    running = (await fetch(pool, "SELECT * FROM turns WHERE turn_id=%s", (turn_id,)))[0]
    assert running["agent_id"] == h.agent_id
    assert running["origin"] == "client"
    assert running["status"] == "running"
    assert running["completed_at"] is None
    assert (await status_row(pool, h.agent_id))["current_turn_id"] == turn_id

    await h.fake.complete_turn(turn_id, "all done")
    await h.stored_through(h.last_fake_seq())
    done = (await fetch(pool, "SELECT * FROM turns WHERE turn_id=%s", (turn_id,)))[0]
    assert done["status"] == "completed"
    assert done["final_text"] == "all done"
    assert done["completed_at"] is not None
    assert done["started_at"] == running["started_at"]
    assert (await status_row(pool, h.agent_id))["current_turn_id"] is None

    # usage travels on the terminal record; a failed turn projects too
    record = {
        "turn_id": "turn-failed-1",
        "session_id": h.session_id,
        "origin": "peer",
        "state": "interrupted",
        "text": "x",
        "final_text": "partial",
        "usage": {"input_tokens": 10, "output_tokens": 3},
        "started_at": "2026-10-23T10:00:00+00:00",
        "completed_at": "2026-10-23T10:00:05+00:00",
    }
    h.fake.emit("turn_failed", {"record": record})
    h.fake.emit(
        "turn_completed", {"record": {**record, "turn_id": "turn-ok-1", "state": "completed"}}
    )
    await h.stored_through(h.last_fake_seq())
    failed = (await fetch(pool, "SELECT * FROM turns WHERE turn_id='turn-failed-1'"))[0]
    assert failed["status"] == "interrupted"
    assert failed["origin"] == "peer"
    assert failed["final_text"] == "partial"
    assert failed["usage"] == {"input_tokens": 10, "output_tokens": 3}
    assert failed["started_at"] == datetime.fromisoformat(record["started_at"])
    assert failed["completed_at"] == datetime.fromisoformat(record["completed_at"])
    ok = (await fetch(pool, "SELECT * FROM turns WHERE turn_id='turn-ok-1'"))[0]
    assert ok["status"] == "completed" and ok["usage"] == {"input_tokens": 10, "output_tokens": 3}


async def test_user_message_stored_as_event_row(harness, pool) -> None:
    h = await harness()
    await h.start()
    await h.stored_through(h.last_fake_seq())

    h.fake.emit(
        "user_message",
        {"turn_id": "turn-peer-1", "origin": "peer", "text": "hi from b", "files": []},
    )
    h.fake.emit(
        "user_message", {"turn_id": None, "origin": "command", "text": "/compact", "files": []}
    )
    h.fake.emit("user_message", {"turn_id": "turn-r", "origin": "restart", "text": "continue"})
    await h.stored_through(h.last_fake_seq())

    rows = [r for r in await events_rows(pool, h.agent_id) if r["type"] == "user_message"]
    assert [r["data"]["origin"] for r in rows] == ["peer", "command", "restart"]
    assert rows[0]["data"]["text"] == "hi from b"
    assert rows[0]["turn_id"] == "turn-peer-1"
    assert rows[1]["turn_id"] is None
    assert await fetch(pool, "SELECT * FROM turns WHERE agent_id=%s", (h.agent_id,)) == []


async def test_reconnect_backoff_after_disconnect(harness, pool) -> None:
    h = await harness()
    await h.start()
    await h.stored_through(h.last_fake_seq())

    await h.fake.set_unreachable(True)
    await wait_until(lambda: len(h.sleeps) >= 4)
    assert h.sleeps[:4] == [1.0, 2.0, 4.0, 8.0]
    await wait_until(lambda: len(h.sleeps) >= 7)
    assert h.sleeps[5:7] == [30.0, 30.0]  # capped

    opened = len(h.opened)
    await h.fake.set_unreachable(False)
    await wait_until(lambda: len(h.opened) > opened)
    h.fake.emit("assistant_text", {"text": "back"})
    await h.stored_through(h.last_fake_seq())

    # a received event reset the backoff: the next drop waits 1 s again
    n = len(h.sleeps)
    h.fake.drop_streams()
    await wait_until(lambda: len(h.sleeps) > n)
    assert h.sleeps[n] == 1.0


async def test_agent_removed_stops_stream(harness, pool) -> None:
    h = await harness()
    ingester = await h.start()
    await h.stored_through(h.last_fake_seq())
    assert h.agent_id in ingester.agent_ids()
    ses = h.fake.sessions[h.session_id]
    await wait_until(lambda: len(ses.subscribers) == 1)

    async with pool.connection() as conn:
        await conn.execute("UPDATE agents SET gone_at=now() WHERE id=%s", (h.agent_id,))
    await wait_until(lambda: h.agent_id not in ingester.agent_ids())
    await wait_until(lambda: len(ses.subscribers) == 0)

    seq = h.last_fake_seq()
    h.fake.emit("assistant_text", {"text": "nobody listens"})
    await asyncio.sleep(0.3)
    assert (await status_row(pool, h.agent_id))["last_seq"] == seq
    assert ingester.agent_ids() == set()


async def test_new_session_restarts_stream(harness, pool) -> None:
    h = await harness()
    ingester = await h.start()
    await h.stored_through(h.last_fake_seq())

    snap = await h.client.create_session("a0-again", "w")
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE agents SET session_id=%s WHERE id=%s", (snap.session_id, h.agent_id)
        )

    async def stored_new() -> bool:
        rows = await fetch(
            pool, "SELECT count(*) AS n FROM events WHERE session_id=%s", (snap.session_id,)
        )
        return rows[0]["n"] >= 2

    await wait_until(stored_new)
    assert ingester.agent_ids() == {h.agent_id}


async def test_session_closed_marks_dead_and_stops(harness, pool, listener) -> None:
    h = await harness()
    ingester = await h.start()
    await h.stored_through(h.last_fake_seq())
    opened = len(h.opened)

    h.fake.emit("session_closed", {})
    await h.stored_through(h.last_fake_seq())
    assert (await status_row(pool, h.agent_id))["state"] == "dead"
    await asyncio.sleep(0.3)
    assert len(h.opened) == opened  # no reconnect until a new session is recorded
    assert h.agent_id in ingester.agent_ids()


async def test_peers_view_recorded(harness, pool) -> None:
    members = [
        {
            "instance": "agent-0",
            "url": "http://agent-0:8080",
            "sessions": [{"session_id": "s0", "name": "a0", "cwd": "/w"}],
            "last_seen_at": "2026-10-23T10:00:00Z",
            "missed": 0,
        },
        {
            "instance": "agent-1",
            "url": "http://agent-1:8080",
            "sessions": [],
            "last_seen_at": None,
            "missed": 2,
        },
    ]
    view = {"enabled": True, "self": {"instance": "agent-0", "url": "http://a0"}}
    h = await harness(peers={**view, "members": members})
    await h.start()
    await h.stored_through(h.last_fake_seq())

    async def recorded() -> bool:
        return len((await status_row(pool, h.agent_id))["peers"]) == 2

    await wait_until(recorded)
    assert (await status_row(pool, h.agent_id))["peers"] == members

    # peers are refreshed on a transition into idle; a disabled mesh counts 0 even when the
    # view still lists members
    h.fake.peers = {"enabled": False, "self": None, "members": members}
    h.fake.emit("session_state", {"state": "busy", "since": OLD_SINCE})
    h.fake.emit("session_state", {"state": "ready", "since": OLD_SINCE})

    async def zero() -> bool:
        return (await status_row(pool, h.agent_id))["peers"] == []

    await wait_until(zero)
    assert status.peer_count({"enabled": False}) == 0
    assert status.peer_count({"enabled": False, "members": members}) == 0
    assert status.peer_count({"enabled": True, "members": members}) == 2
