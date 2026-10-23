"""One contract suite for the wrapper API, run against FakeWrapper and the real wrapper image.

The `real` leg needs ORCH_TEST_WRAPPER_IMAGE (see tests/fakes/real_wrapper.py) and is marked
`integration`. Both legs run the same assertions, so the fake cannot drift from the wrapper.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fakes.fake_wrapper import FakeWrapper
from fakes.real_wrapper import RealWrapper

from agent_orchestrator.wrapper import (
    Ambiguous,
    Busy,
    ConfigError,
    Gone,
    Rejected,
    TurnAccepted,
    Unavailable,
    WrapperClient,
)

TOKEN = "ci-token"
SLOW = "slow please"  # the real leg's scenario sleeps for a few seconds on this prompt
TIMEOUT = 40.0


@pytest.fixture(scope="session")
def real_wrapper() -> Iterator[RealWrapper]:
    image = os.environ["ORCH_TEST_WRAPPER_IMAGE"]  # integration leg: required, KeyError if unset
    wrapper = RealWrapper(image, token=TOKEN)
    wrapper.start()
    try:
        yield wrapper
    finally:
        wrapper.stop()


@dataclass
class Leg:
    kind: str
    base_url: str
    fake: FakeWrapper | None
    http: httpx.AsyncClient
    sessions: list[str]

    def client(self, **kw: Any) -> WrapperClient:
        return WrapperClient(self.base_url, TOKEN, **kw)

    async def create(self, name: str = "contract") -> str:
        resp = await self.http.post("/v1/sessions", json={"name": name}, timeout=120)
        assert resp.status_code == 201, resp.text
        sid = resp.json()["session_id"]
        self.sessions.append(sid)
        return sid

    async def submit(self, sid: str, text: str = "hello", **extra: Any) -> httpx.Response:
        return await self.http.post(f"/v1/sessions/{sid}/turns", json={"text": text, **extra})

    async def finish(self, sid: str, turn_id: str) -> None:
        """Let the turn end: the fake completes on demand, the real fake claude on its own."""
        if self.fake is not None:
            await self.fake.complete_turn(turn_id, "done")
        await self.wait_turn(sid, turn_id)

    async def wait_turn(self, sid: str, turn_id: str) -> dict[str, Any]:
        end = time.monotonic() + TIMEOUT
        while True:
            rec = (await self.http.get(f"/v1/sessions/{sid}/turns/{turn_id}")).json()
            if rec["state"] != "running":
                return rec
            assert time.monotonic() < end, "turn did not finish"
            await asyncio.sleep(0.1)

    async def wait_ready(self, sid: str) -> dict[str, Any]:
        end = time.monotonic() + TIMEOUT
        while True:
            snap = (await self.http.get(f"/v1/sessions/{sid}")).json()
            if snap["state"] == "ready":
                return snap
            assert time.monotonic() < end, f"session stuck in {snap['state']}"
            await asyncio.sleep(0.1)

    async def turn(self, sid: str, text: str = "hello", **extra: Any) -> str:
        resp = await self.submit(sid, text, **extra)
        assert resp.status_code == 202, resp.text
        turn_id = resp.json()["turn_id"]
        await self.finish(sid, turn_id)
        await self.wait_ready(sid)
        return turn_id

    async def history(self, sid: str, **params: Any) -> httpx.Response:
        return await self.http.get(f"/v1/sessions/{sid}/history", params=params)

    async def collect(self, sid: str, done: Any, after: int | None = None) -> list[Any]:
        """Follow the SSE stream until `done(events)` is true."""
        client = self.client()
        out: list[Any] = []
        try:
            async with asyncio.timeout(TIMEOUT):
                async for ev in client.events(sid, last_event_id=after):
                    out.append(ev)
                    if done(out):
                        break
        finally:
            await client.aclose()
        return out


@pytest.fixture(params=["fake", pytest.param("real", marks=pytest.mark.integration)])
async def leg(request: pytest.FixtureRequest) -> AsyncIterator[Leg]:
    fake: FakeWrapper | None = None
    if request.param == "fake":
        fake = FakeWrapper(token=TOKEN)
        await fake.start()
        base_url = fake.base_url
    else:
        base_url = request.getfixturevalue("real_wrapper").base_url
    http = httpx.AsyncClient(
        base_url=base_url, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30
    )
    state = Leg(request.param, base_url, fake, http, [])
    try:
        yield state
    finally:
        for sid in state.sessions:
            await http.delete(f"/v1/sessions/{sid}", params={"purge": "true"})
        await http.aclose()
        if fake is not None:
            await fake.stop()


async def test_turn_lifecycle_events(leg: Leg) -> None:
    sid = await leg.create()
    await leg.turn(sid)
    events = await leg.collect(sid, lambda evs: "turn_completed" in {e.type for e in evs}, after=0)
    seqs = [e.seq for e in events]
    assert seqs == sorted(set(seqs))
    marks = [
        e.type if e.type != "session_state" else f"state:{e.data['state']}"
        for e in events
        if e.type in ("user_message", "turn_completed", "session_state")
    ]
    tail = marks[marks.index("state:busy") :]
    # the wrapper logs busy, then the prompt, and the idle (ready) transition around the end
    assert tail[:2] == ["state:busy", "user_message"]
    assert set(tail[2:]) == {"turn_completed", "state:ready"}
    ready = [e for e in events if e.type == "session_state" and e.data["state"] == "ready"][-1]
    assert ready.data["since"]
    done = next(e for e in events if e.type == "turn_completed")
    assert done.data["record"]["state"] == "completed"
    assert done.data["record"]["turn_id"] in {
        e.data.get("turn_id") for e in events if e.type == "user_message"
    }


async def test_busy_session_returns_409(leg: Leg) -> None:
    sid = await leg.create()
    first = await leg.submit(sid, SLOW)
    assert first.status_code == 202
    turn_id = first.json()["turn_id"]
    assert first.json()["state"] == "running"
    second = await leg.submit(sid, "again")
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "busy"
    assert isinstance(await leg.client().submit_turn(sid, "again"), Busy)
    await leg.finish(sid, turn_id)


async def test_last_event_id_replay(leg: Leg) -> None:
    sid = await leg.create()
    await leg.turn(sid)
    all_events = (await leg.history(sid, limit=1000)).json()["events"]
    last = all_events[-1]["seq"]
    cut = all_events[len(all_events) // 2]["seq"]
    replayed = await leg.collect(sid, lambda evs: evs[-1].seq >= last, after=cut)
    assert [e.seq for e in replayed] == [e["seq"] for e in all_events if e["seq"] > cut]
    assert all(e.seq > cut for e in replayed)
    bad = await leg.http.get(f"/v1/sessions/{sid}/events", headers={"Last-Event-ID": "nope"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_last_event_id"


async def test_snapshot_fields(leg: Leg) -> None:
    sid = await leg.create()
    snap = await leg.wait_ready(sid)
    assert "context" in snap  # null until the first assistant message
    assert snap["idle_since"]
    assert snap["current_turn_id"] is None
    resp = await leg.submit(sid, SLOW)
    busy = (await leg.http.get(f"/v1/sessions/{sid}")).json()
    assert busy["state"] == "busy"
    assert busy["idle_since"] is None
    assert busy["current_turn_id"] == resp.json()["turn_id"]
    await leg.finish(sid, resp.json()["turn_id"])
    after = await leg.wait_ready(sid)
    assert after["idle_since"] and after["idle_since"] >= snap["idle_since"]
    typed = await leg.client().get_session(sid)
    assert typed.idle_since is not None
    listed = (await leg.http.get("/v1/sessions")).json()["sessions"]
    assert sid in [s["session_id"] for s in listed]


async def test_peers_view_and_auth(leg: Leg) -> None:
    peers = await leg.http.get("/v1/peers")
    assert peers.status_code == 200
    assert isinstance(peers.json()["enabled"], bool)
    typed = await leg.client().peers()
    assert typed.enabled == peers.json()["enabled"]
    for headers in ({"Authorization": "Bearer wrong"}, {}):
        async with httpx.AsyncClient(base_url=leg.base_url, headers=headers) as anon:
            resp = await anon.get("/v1/sessions")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"
        assert resp.json()["error"]["message"]


async def test_history_backward(leg: Leg) -> None:
    sid = await leg.create()
    await leg.turn(sid)
    types = "user_message,turn_completed"
    big = 10**9
    full = (await leg.history(sid, before_seq=big, types=types, limit=100)).json()
    assert [e["type"] for e in full["events"]] == ["user_message", "turn_completed"]
    seqs = [e["seq"] for e in full["events"]]
    assert seqs == sorted(seqs)
    assert full["has_more_before"] is False
    assert full["prev_seq"] is None
    page = (await leg.history(sid, before_seq=big, types=types, limit=1)).json()
    assert [e["type"] for e in page["events"]] == ["turn_completed"]
    assert page["has_more_before"] is True
    older = (await leg.history(sid, before_seq=page["prev_seq"], types=types, limit=1)).json()
    assert [e["type"] for e in older["events"]] == ["user_message"]
    assert older["has_more_before"] is False
    assert older["prev_seq"] is None
    forward = (await leg.history(sid, after_seq=0, limit=1000)).json()
    assert "prev_seq" not in forward
    bad = await leg.history(sid, before_seq=big, types="user_message,nonsense")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_request"
    both = await leg.history(sid, before_seq=big, after_seq=0)
    assert both.status_code == 400


async def test_resume_stopped_session(leg: Leg) -> None:
    with_transcript = await leg.create("with-transcript")
    await leg.turn(with_transcript)
    bare = await leg.create("no-transcript")
    for sid in (with_transcript, bare):
        stopped = await leg.http.delete(f"/v1/sessions/{sid}")
        assert stopped.status_code == 200, stopped.text
        assert (await leg.http.get(f"/v1/sessions/{sid}")).json()["state"] == "stopped"
    refused = await leg.http.post(f"/v1/sessions/{bare}/resume")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "not_resumable"
    resumed = await leg.http.post(f"/v1/sessions/{with_transcript}/resume", timeout=120)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "ready"
    typed = await leg.client().get_session(with_transcript)
    assert typed.state == "ready"
    missing = await leg.http.post("/v1/sessions/ses_nope/resume")
    assert missing.status_code == 404


async def test_turn_metadata_on_record(leg: Leg) -> None:
    sid = await leg.create()
    plain = await leg.turn(sid, "plain")
    tagged = await leg.turn(sid, "tagged", metadata={"orch_message_id": "msg-7"})
    rec = (await leg.http.get(f"/v1/sessions/{sid}/turns/{tagged}")).json()
    assert rec["metadata"] == {"orch_message_id": "msg-7"}
    assert rec["state"] == "completed"
    assert rec["text"] == "tagged"
    assert (await leg.http.get(f"/v1/sessions/{sid}/turns/{plain}")).json()["metadata"] == {}
    listed = (await leg.http.get(f"/v1/sessions/{sid}/turns")).json()["turns"]
    assert [t["turn_id"] for t in listed] == [plain, tagged]
    typed = await leg.client().get_turn(sid, tagged)
    assert typed.metadata == {"orch_message_id": "msg-7"}
    assert [t.turn_id for t in await leg.client().list_turns(sid)] == [plain, tagged]
    missing = await leg.http.get(f"/v1/sessions/{sid}/turns/turn-nope")
    assert missing.status_code == 404


# ---------------------------------------------------------------- fake-only controls


@pytest.fixture
async def fake() -> AsyncIterator[FakeWrapper]:
    wrapper = FakeWrapper(token=TOKEN)
    await wrapper.start()
    try:
        yield wrapper
    finally:
        await wrapper.stop()


async def _session(fake: FakeWrapper) -> tuple[WrapperClient, str]:
    client = WrapperClient(fake.base_url, TOKEN, send_timeout=0.5)
    snap = await client.create_session("c", "c")
    return client, snap.session_id


async def test_fake_status_delay_and_reset_controls(fake: FakeWrapper) -> None:
    client, sid = await _session(fake)
    try:
        expected = {
            404: Gone,
            410: Gone,
            401: ConfigError,
            403: ConfigError,
            413: Rejected,
            422: Rejected,
            500: Ambiguous,
            502: Ambiguous,
            503: Unavailable,
            504: Unavailable,
        }
        for code, kind in expected.items():
            fake.set_turn_status(code)
            assert isinstance(await client.submit_turn(sid, "x"), kind), code
        assert fake.received_turns == 0
        fake.set_turn_status(None)

        fake.delay_turn_response(2.0)
        out = await client.submit_turn(sid, "slow", metadata={"orch_message_id": "m1"})
        assert isinstance(out, Ambiguous)
        assert fake.received_turns == 1  # recorded although the client gave up
        assert fake.turn_texts == ["slow"]
        fake.delay_turn_response(0)
        await fake.complete_turn(fake.last_turn_id, "ok")

        fake.reset_connection_after_receive()
        out = await client.submit_turn(sid, "reset")
        assert isinstance(out, Ambiguous)
        assert fake.received_turns == 2
        await fake.complete_turn(fake.last_turn_id, "ok")

        fake.reset_connection_after_receive(False)
        out = await client.submit_turn(sid, "fine")
        assert isinstance(out, TurnAccepted)
        assert fake.received_turns == 3
        assert fake.turn_metadata[0] == {"orch_message_id": "m1"}
    finally:
        await client.aclose()


async def test_fake_ambiguity_controls(fake: FakeWrapper) -> None:
    client, sid = await _session(fake)
    try:
        fake.reset_connection_before_receive()
        out = await client.submit_turn(sid, "lost")
        assert isinstance(out, Ambiguous)
        assert fake.received_turns == 0  # the wrapper never saw the turn
        assert (await client.get_session(sid)).state == "ready"
        fake.reset_connection_before_receive(False)

        await fake.set_unreachable(True)
        out = await client.submit_turn(sid, "nobody home")
        assert isinstance(out, Unavailable)
        assert fake.received_turns == 0
        await fake.set_unreachable(False)

        out = await client.submit_turn(sid, "back")
        assert isinstance(out, TurnAccepted)
        assert fake.received_turns == 1
    finally:
        await client.aclose()


async def test_fake_states_and_controls(fake: FakeWrapper) -> None:
    client, sid = await _session(fake)
    try:
        fake.set_booting(True)
        out = await client.submit_turn(sid, "x")
        assert isinstance(out, Unavailable)
        fake.set_booting(False)

        fake.set_session_state("failed", has_transcript=True)
        assert isinstance(await client.submit_turn(sid, "x"), Unavailable)
        assert (await client.get_session(sid)).state == "failed"
        assert (await client.resume_session(sid)).state == "ready"
        fake.set_session_state("stopped", has_transcript=False)
        with pytest.raises(Exception, match="not_resumable"):
            await client.resume_session(sid)

        fake.set_session_state("ready", has_transcript=True)
        fake.set_context(used_tokens=100, window_tokens=1000)
        snap = await client.get_session(sid)
        assert snap.context is not None
        assert snap.context.used_percentage == 10.0

        seen: list[Any] = []

        async def follow() -> None:
            async for ev in client.events(sid):
                seen.append(ev)

        task = asyncio.create_task(follow())
        await asyncio.sleep(0.2)
        fake.emit("assistant_text", {"text": "hi"}, session_id=sid)
        await asyncio.sleep(0.2)
        assert seen[-1].type == "assistant_text"
        fake.drop_streams()
        with pytest.raises(Exception):  # noqa: B017 - the dropped stream ends or errors
            await asyncio.wait_for(task, 5)
            raise RuntimeError("stream ended")
        before = seen[-1].seq
        fake.skip_seq(10)
        fake.emit("assistant_text", {"text": "after rotation"}, session_id=sid)
        replay = [ev async for ev in _take(client, sid, before, 1)]
        assert replay[0].seq == before + 11
    finally:
        await client.aclose()


async def _take(client: WrapperClient, sid: str, after: int, n: int) -> AsyncIterator[Any]:
    count = 0
    async for ev in client.events(sid, last_event_id=after):
        yield ev
        count += 1
        if count >= n:
            return
