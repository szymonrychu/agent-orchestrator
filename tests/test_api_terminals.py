"""Terminal proxy routes: POST/GET/DELETE under /api/v1 forwarded to the agent wrappers (D4, D5)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest_asyncio
from factories import insert_cluster
from fakes.fake_wrapper import FakeWrapper
from test_api_clusters import assert_error, client_for, make_settings
from test_api_files import KEY, Rig, token_of

from agents_orchestrator.api import workspace
from agents_orchestrator.api.app import create_app

GOOD_ORIGIN = "https://orch.example"
VIEW_FIELDS = {
    "agent_id",
    "cluster_id",
    "pod_name",
    "terminal_id",
    "created_at",
    "alive",
    "exit_code",
    "attached",
    "rows",
    "cols",
}


@dataclass
class TermRig(Rig):
    app: object = None

    def create_url(self, row: dict) -> str:
        return f"/api/v1/agents/{row['id']}/terminals"

    def item_url(self, row: dict, terminal_id: str) -> str:
        return f"/api/v1/agents/{row['id']}/terminals/{terminal_id}"

    def list_url(self, cluster: dict | None = None) -> str:
        return f"/api/v1/clusters/{(cluster or self.cluster)['id']}/terminals"


@pytest_asyncio.fixture
async def rig(pool, dsn) -> AsyncIterator[TermRig]:
    settings = make_settings(dsn, master_key=KEY, allowed_origins=GOOD_ORIGIN)
    app = create_app(settings, pool)
    cluster = await insert_cluster(pool)
    state = TermRig(None, pool, cluster, [], app)
    async with client_for(app) as http:  # no lifespan runs: ASGITransport never starts it
        state.http = http
        try:
            yield state
        finally:
            await workspace.close_client(app)
            for fake in state.fakes:
                await fake.stop()


def terminal_calls(fake: FakeWrapper) -> list[tuple[str, str]]:
    return [call for call in fake.requests if "/terminals" in call[1]]


async def test_create_on_agent(rig):
    await rig.agent(0)
    fake, sid, row = await rig.agent(1)

    r = await rig.http.post(rig.create_url(row), json={"rows": 30, "cols": 100})
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == VIEW_FIELDS
    assert body["agent_id"] == str(row["id"])
    assert body["cluster_id"] == str(rig.cluster["id"])
    assert body["pod_name"] == row["pod_name"]
    assert body["terminal_id"].startswith("term_") and len(body["terminal_id"]) == 17
    assert body["alive"] is True and body["exit_code"] is None and body["attached"] is False
    assert (body["rows"], body["cols"]) == (30, 100)
    assert body["created_at"]
    # the request reached that agent's session
    assert list(fake.terminals) == [body["terminal_id"]]
    assert fake.terminals[body["terminal_id"]].session_id == sid
    assert terminal_calls(fake) == [("POST", f"/v1/sessions/{sid}/terminals")]

    # no body at all: the wrapper defaults apply
    r = await rig.http.post(rig.create_url(row))
    assert r.status_code == 201, r.text
    assert (r.json()["rows"], r.json()["cols"]) == (24, 80)

    # extra keys are refused by the orchestrator itself, nothing is forwarded
    before = len(terminal_calls(fake))
    r = await rig.http.post(rig.create_url(row), json={"rows": 5, "shell": "/bin/zsh"})
    assert_error(r, 422, "invalid_request")
    assert len(terminal_calls(fake)) == before


async def test_create_errors(rig):
    fake, _, row = await rig.agent(0)
    assert_error(await rig.http.post(f"/api/v1/agents/{uuid.uuid4()}/terminals"), 404, "not_found")
    _, _, pending = await rig.agent(1, ready=False)
    assert_error(await rig.http.post(rig.create_url(pending)), 409, "agent_not_ready")
    await fake.set_unreachable(True)
    r = await rig.http.post(rig.create_url(row))
    assert_error(r, 502, "wrapper_unreachable")
    await fake.set_unreachable(False)


async def test_cluster_list_fans_out(rig):
    first, _, row1 = await rig.agent(0)
    second, _, row2 = await rig.agent(1)
    broken, _, row3 = await rig.agent(2)
    idle, _, _ = await rig.agent(3, ready=False)  # never ready: not queried
    made = {}
    for row in (row1, row2, row3):
        r = await rig.http.post(rig.create_url(row), json={})
        assert r.status_code == 201, r.text
        made[row["id"]] = r.json()
    extra = (await rig.http.post(rig.create_url(row2))).json()
    await broken.set_unreachable(True)

    r = await rig.http.get(rig.list_url())
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"terminals", "unavailable_agents"}
    assert body["unavailable_agents"] == [str(row3["id"])]
    got = {t["terminal_id"]: t for t in body["terminals"]}
    assert set(got) == {made[row1["id"]]["terminal_id"], made[row2["id"]]["terminal_id"]} | {
        extra["terminal_id"]
    }
    assert all(set(t) == VIEW_FIELDS for t in got.values())
    assert got[extra["terminal_id"]]["agent_id"] == str(row2["id"])
    assert got[made[row1["id"]]["terminal_id"]]["pod_name"] == row1["pod_name"]
    assert [c[0] for c in terminal_calls(first)] == ["POST", "GET"]
    assert terminal_calls(idle) == []
    await broken.set_unreachable(False)

    # a healthy cluster reports nobody unavailable; an unknown cluster is 404
    again = (await rig.http.get(rig.list_url())).json()
    assert again["unavailable_agents"] == [] and len(again["terminals"]) == 4
    assert_error(await rig.http.get(f"/api/v1/clusters/{uuid.uuid4()}/terminals"), 404, "not_found")


async def test_cluster_list_without_ready_agent_is_empty(rig):
    await rig.agent(0, ready=False)
    r = await rig.http.get(rig.list_url())
    assert r.status_code == 200 and r.json() == {"terminals": [], "unavailable_agents": []}


async def test_delete(rig):
    fake, sid, row = await rig.agent(0)
    tid = (await rig.http.post(rig.create_url(row))).json()["terminal_id"]

    r = await rig.http.delete(rig.item_url(row, tid))
    assert r.status_code == 204 and r.content == b""
    assert tid not in fake.terminals
    assert (fake.requests[-1]) == ("DELETE", f"/v1/sessions/{sid}/terminals/{tid}")
    listed = (await rig.http.get(rig.list_url())).json()
    assert listed["terminals"] == []

    again = await rig.http.delete(rig.item_url(row, tid))
    assert_error(again, 404, "terminal_not_found")
    unknown = await rig.http.delete(rig.item_url(row, "term_0123456789ab"))
    assert_error(unknown, 404, "terminal_not_found")
    assert_error(
        await rig.http.delete(f"/api/v1/agents/{uuid.uuid4()}/terminals/{tid}"), 404, "not_found"
    )


async def test_limit_passes_through(rig):
    fake, _, row = await rig.agent(0)
    fake.terminals_max = 2
    for _ in range(2):
        assert (await rig.http.post(rig.create_url(row))).status_code == 201
    assert_error(await rig.http.post(rig.create_url(row)), 409, "terminal_limit")
    assert len(fake.terminals) == 2


async def test_invalid_size_passes_through(rig):
    fake, _, row = await rig.agent(0)
    for body in ({"rows": 0}, {"cols": 1001}, {"rows": 1001, "cols": 80}, {"rows": -3}):
        assert_error(await rig.http.post(rig.create_url(row), json=body), 422, "invalid_size")
    assert fake.terminals == {}
    for body in ({"rows": 1, "cols": 1000}, {"rows": 1000}):
        assert (await rig.http.post(rig.create_url(row), json=body)).status_code == 201


async def test_session_not_active_passes_through(rig):
    fake, sid, row = await rig.agent(0)
    fake.set_session_state("stopped", session_id=sid)
    assert_error(await rig.http.post(rig.create_url(row)), 409, "session_not_active")


async def test_invalid_terminal_id_rejected(rig):
    fake, _, row = await rig.agent(0)
    good = (await rig.http.post(rig.create_url(row))).json()["terminal_id"]
    seen = len(fake.requests)
    for bad in (
        "term_zz",
        "term_0123456789AB",  # uppercase
        "term_0123456789abc",  # too long
        "term_0123456789a",  # too short
        "terminal",
        "TERM_0123456789ab",
        "term_0123456789ab%0A",  # a trailing newline must not pass `$`
        "x" * 300,
    ):
        r = await rig.http.delete(rig.item_url(row, bad))
        assert_error(r, 422, "invalid_request")
    assert len(fake.requests) == seen  # the wrapper was never called
    assert good in fake.terminals


async def test_origin_guard(rig):
    fake, _, row = await rig.agent(0)
    tid = (await rig.http.post(rig.create_url(row))).json()["terminal_id"]
    seen = len(fake.requests)

    for bad in ("https://evil.example", "null", "http://orch.example", "https://orch.example:8443"):
        headers = {"Origin": bad}
        r = await rig.http.post(rig.create_url(row), headers=headers)
        assert_error(r, 403, "forbidden_origin")
        r = await rig.http.delete(rig.item_url(row, tid), headers=headers)
        assert_error(r, 403, "forbidden_origin")
    assert len(fake.requests) == seen and tid in fake.terminals

    # reads are not state changing: not guarded
    r = await rig.http.get(rig.list_url(), headers={"Origin": "https://evil.example"})
    assert r.status_code == 200

    # a listed Origin (case-insensitive) and no Origin at all pass
    ok = await rig.http.post(rig.create_url(row), headers={"Origin": GOOD_ORIGIN.upper()})
    assert ok.status_code == 201, ok.text
    gone = await rig.http.delete(rig.item_url(row, tid), headers={"Origin": GOOD_ORIGIN})
    assert gone.status_code == 204
    assert (await rig.http.post(rig.create_url(row))).status_code == 201


async def test_token_not_in_logs_or_responses(rig, caplog):
    fake, _, row = await rig.agent(0)
    token = token_of(rig.cluster["id"])
    caplog.set_level(logging.DEBUG)
    responses = []
    created = await rig.http.post(rig.create_url(row), json={"rows": 10, "cols": 20})
    responses.append(created)
    tid = created.json()["terminal_id"]
    responses.append(await rig.http.get(rig.list_url()))
    responses.append(await rig.http.delete(rig.item_url(row, tid)))
    # error paths too: a wrapper that refuses the bearer, and one that is gone
    fake.token = "different"
    responses.append(await rig.http.post(rig.create_url(row)))
    responses.append(await rig.http.get(rig.list_url()))
    fake.token = token
    await fake.set_unreachable(True)
    responses.append(await rig.http.post(rig.create_url(row)))
    responses.append(await rig.http.get(rig.list_url()))
    await fake.set_unreachable(False)

    assert [r.status_code for r in responses[:3]] == [201, 200, 204]
    assert caplog.records, "DEBUG logging should have produced records"
    for record in caplog.records:
        assert token not in record.getMessage()
        assert token not in str(record.args) and token not in (record.exc_text or "")
    for resp in responses:
        assert token not in resp.text
        assert token not in str(dict(resp.headers))


async def test_routes_work_without_lifespan(rig):
    """No app lifespan runs here: the shared wrapper client is created on first use."""
    fake, _, row = await rig.agent(0)
    assert rig.app.state.wrapper_http is None
    r = await rig.http.post(rig.create_url(row))
    assert r.status_code == 201, r.text
    client = rig.app.state.wrapper_http
    assert client is not None
    assert (await rig.http.get(rig.list_url())).status_code == 200
    assert rig.app.state.wrapper_http is client  # one shared client
    assert (await rig.http.delete(rig.item_url(row, r.json()["terminal_id"]))).status_code == 204
