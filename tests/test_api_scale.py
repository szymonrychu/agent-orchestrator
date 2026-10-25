"""The public scale route: POST /api/v1/sessions/{id}/scale."""

from __future__ import annotations

import uuid

import pytest
from factories import insert_agent, insert_session
from test_api_sessions import assert_error, client_for, make_settings

from agents_orchestrator.api.app import create_app


@pytest.fixture
async def http(pool, dsn):
    async with client_for(create_app(make_settings(dsn), pool)) as c:
        yield c


def url(c) -> str:
    return f"/api/v1/sessions/{c['id']}/scale"


async def test_scale_returns_202_and_updates_view(pool, http):
    c = await insert_session(pool, spec={"replicas": 2, "max_replicas": 4})
    r = await http.post(url(c), json={"replicas": 4})
    assert r.status_code == 202, r.text
    assert r.json() == {
        "session_id": str(c["id"]),
        "replicas": 4,
        "previous_replicas": 2,
        "max_replicas": 4,
    }
    view = (await http.get(f"/api/v1/sessions/{c['id']}")).json()
    assert view["spec"]["replicas"] == 4
    assert view["generation"] == c["generation"]
    r = await http.post(url(c), json={"replicas": 1})
    assert r.status_code == 202, r.text
    assert r.json()["previous_replicas"] == 4
    view = (await http.get(f"/api/v1/sessions/{c['id']}")).json()
    assert view["spec"]["replicas"] == 1
    assert view["generation"] == c["generation"]


async def test_scale_refusals_map_to_error_codes(pool, http):
    static = await insert_session(pool, spec={"replicas": 2, "max_replicas": 2, "static": True})
    assert_error(await http.post(url(static), json={"replicas": 1}), 409, "static_session")
    hive = await insert_session(pool, spec={"replicas": 2, "max_replicas": 3})
    assert_error(await http.post(url(hive), json={"replicas": 4}), 422, "out_of_range")
    assert_error(await http.post(url(hive), json={"replicas": 0}), 422, "out_of_range")
    gone = await insert_session(pool, spec={"replicas": 2, "max_replicas": 3}, desired="deleting")
    assert_error(await http.post(url(gone), json={"replicas": 3}), 409, "session_deleting")
    missing = await http.post(f"/api/v1/sessions/{uuid.uuid4()}/scale", json={"replicas": 1})
    assert_error(missing, 404, "not_found")
    assert_error(await http.post(url(hive), json={"replicas": "x"}), 422, "invalid_request")
    assert_error(await http.post(url(hive), json={}), 422, "invalid_request")
    extra = await http.post(url(hive), json={"replicas": 2, "foo": 1})
    assert_error(extra, 422, "invalid_request")


async def test_scale_leader_would_leave(pool, http):
    c = await insert_session(pool, spec={"replicas": 3, "max_replicas": 4})
    await insert_agent(pool, c["id"], ordinal=0, ready=True)
    await insert_agent(pool, c["id"], ordinal=1, ready=True)
    a2 = await insert_agent(pool, c["id"], ordinal=2, ready=True)
    async with pool.connection() as conn:
        await conn.execute(
            "INSERT INTO session_leaders (session_id, agent_id, epoch, since, reason)"
            " VALUES (%s, %s, 1, now(), 'start')",
            (c["id"], a2["id"]),
        )
    assert_error(await http.post(url(c), json={"replicas": 2}), 409, "leader_would_leave")
    view = (await http.get(f"/api/v1/sessions/{c['id']}")).json()
    assert view["spec"]["replicas"] == 3
