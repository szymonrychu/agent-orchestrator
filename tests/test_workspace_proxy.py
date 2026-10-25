"""api/workspace.py: the one seam through which the api calls wrapper pods."""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest
from factories import insert_agent, insert_session, set_agent_status

from agents_orchestrator.api import workspace
from agents_orchestrator.api.app import create_app
from agents_orchestrator.api.errors import ApiError
from agents_orchestrator.api.workspace import (
    TERMINAL_ID_RE,
    WrapperTarget,
    forward,
    get_client,
    resolve_agent,
    validate_terminal_id,
)
from agents_orchestrator.settings import Settings
from agents_orchestrator.tokens import derive_tokens

KEY = "k" * 32


def make_settings(dsn="postgresql://unused/unused", **kw) -> Settings:
    kw.setdefault("allowed_cidrs", "10.1.2.0/24")
    kw.setdefault("agent_image", "img:1")
    kw.setdefault("master_key", KEY)
    return Settings(database_url=dsn, **kw)


def token_of(session_id) -> str:
    return derive_tokens(KEY.encode(), session_id).auth


async def ready_agent(pool, session_id, ordinal, *, state="idle", **kw):
    row = await insert_agent(
        pool,
        session_id,
        ordinal=ordinal,
        url=kw.pop("url", f"http://10.0.0.{ordinal + 1}:8080"),
        wrapper_session_id=kw.pop("wrapper_session_id", f"ses_{ordinal}"),
        ready=kw.pop("ready", True),
        **kw,
    )
    await set_agent_status(pool, row["id"], state=state)
    return row


async def resolve(pool, settings, **kw):
    async with pool.connection() as conn:
        return await resolve_agent(conn, settings, **kw)


async def expect(pool, settings, status, code, **kw):
    with pytest.raises(ApiError) as err:
        await resolve(pool, settings, **kw)
    assert (err.value.status, err.value.code) == (status, code)
    return err.value


async def test_resolve_agent_rules(pool, dsn):
    settings = make_settings(dsn)
    c = await insert_session(pool)
    cid = c["id"]
    # ordinal 0: not ready yet; 1: dead; 2: no session; 3: gone; 4: first usable; 5: also usable
    await ready_agent(pool, cid, 0, ready=False)
    await ready_agent(pool, cid, 1, state="dead")
    await ready_agent(pool, cid, 2, wrapper_session_id=None)
    from datetime import UTC, datetime

    await ready_agent(pool, cid, 3, gone_at=datetime.now(UTC))
    a4 = await ready_agent(pool, cid, 4, state="busy")
    a5 = await ready_agent(pool, cid, 5)

    target = await resolve(pool, settings, session_id=cid)
    assert isinstance(target, WrapperTarget)
    assert target.agent_id == a4["id"]
    assert target.session_id == cid
    assert target.pod_name == a4["pod_name"]
    assert target.url == a4["url"]
    assert target.wrapper_session_id == "ses_4"
    assert target.token == token_of(cid)

    # explicit agent wins over ordinal order; session_id optional but checked
    by_id = await resolve(pool, settings, agent_id=a5["id"])
    assert by_id.agent_id == a5["id"] and by_id.session_id == cid
    assert (await resolve(pool, settings, agent_id=a5["id"], session_id=cid)).agent_id == a5["id"]

    # not_found: unknown agent, unknown session, agent of another session
    other = await insert_session(pool)
    await expect(pool, settings, 404, "not_found", agent_id=uuid.uuid4())
    await expect(pool, settings, 404, "not_found", session_id=uuid.uuid4())
    await expect(pool, settings, 404, "not_found", agent_id=a5["id"], session_id=other["id"])

    # agent_not_ready: no usable agent, or the named agent is not usable
    await expect(pool, settings, 409, "agent_not_ready", session_id=other["id"])
    dead = await ready_agent(pool, other["id"], 0, state="dead")
    await expect(pool, settings, 409, "agent_not_ready", session_id=other["id"])
    await expect(pool, settings, 409, "agent_not_ready", agent_id=dead["id"])
    nourl = await ready_agent(pool, other["id"], 1, url=None)
    await expect(pool, settings, 409, "agent_not_ready", agent_id=nourl["id"])

    # a session being discarded is not usable either
    gone = await insert_session(pool, desired="deleting")
    await ready_agent(pool, gone["id"], 0)
    await expect(pool, settings, 409, "agent_not_ready", session_id=gone["id"])

    # no master key: 503 unavailable
    err = await expect(
        pool, make_settings(dsn, master_key=None), 503, "unavailable", session_id=cid
    )
    assert KEY not in err.message


def test_target_repr_hides_token():
    t = WrapperTarget(
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        pod_name="p",
        url="http://w:8080",
        wrapper_session_id="s",
        token="SECRET-TOKEN",
    )
    assert "SECRET-TOKEN" not in repr(t) and "SECRET-TOKEN" not in str(t)


def target_for(token="tok-123", url="http://w:8080") -> WrapperTarget:
    return WrapperTarget(
        agent_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        pod_name="agent-0",
        url=url,
        wrapper_session_id="ses_0",
        token=token,
    )


def app_with(handler) -> object:
    app = create_app(make_settings())
    app.state.wrapper_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return app


async def test_forward_error_mapping():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        p = req.url.path
        assert req.headers["authorization"] == "Bearer tok-123"
        if p.endswith("/ok"):
            return httpx.Response(200, json={"entries": [1]})
        if p.endswith("/bad"):
            return httpx.Response(422, json={"error": {"code": "path_outside", "message": "no"}})
        if p.endswith("/missing"):
            return httpx.Response(404, json={"error": {"code": "not_found", "message": "x"}})
        if p.endswith("/plain"):
            return httpx.Response(400, text="not json")
        if p.endswith("/boom"):
            return httpx.Response(500, json={"error": {"code": "internal", "message": "trace"}})
        if p.endswith("/gw"):
            return httpx.Response(503, text="upstream")
        raise httpx.ConnectError("refused")

    app = app_with(handler)
    t = target_for()
    assert await forward(app, t, "GET", "/ok", params={"p": "a"}) == {"entries": [1]}
    assert calls[0].url.params["p"] == "a"
    assert "tok-123" not in str(calls[0].url)

    for path, status, code in (
        ("/bad", 422, "path_outside"),
        ("/missing", 404, "not_found"),
        ("/plain", 400, "http_400"),
        ("/boom", 502, "wrapper_error"),
        ("/gw", 502, "wrapper_error"),
        ("/down", 502, "wrapper_unreachable"),
    ):
        with pytest.raises(ApiError) as err:
            await forward(app, t, "GET", path)
        assert (err.value.status, err.value.code) == (status, code), path
        assert "tok-123" not in err.value.message
    # a 5xx does not relay the wrapper's body
    with pytest.raises(ApiError) as err:
        await forward(app, t, "GET", "/boom")
    assert "trace" not in err.value.message


async def test_shared_client_lifecycle(pool, dsn):
    app = create_app(make_settings(dsn), pool)
    async with app.router.lifespan_context(app):
        assert getattr(app.state, "wrapper_http", None) is None
        first = get_client(app)
        assert isinstance(first, httpx.AsyncClient)
        assert get_client(app) is first
        assert not first.is_closed
    assert first.is_closed
    # a lifespan that never created a client creates none
    app2 = create_app(make_settings(dsn), pool)
    async with app2.router.lifespan_context(app2):
        pass
    assert getattr(app2.state, "wrapper_http", None) is None


async def test_client_works_without_lifespan(pool, dsn, monkeypatch):
    """No lifespan runs under ASGITransport: the helper builds its client on demand."""
    seen = []

    class Wrapper(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            seen.append(request)
            return httpx.Response(200, json={"hello": "wrapper"})

    monkeypatch.setattr(
        workspace, "new_client", lambda: httpx.AsyncClient(transport=Wrapper(), trust_env=False)
    )
    app = create_app(make_settings(dsn), pool)
    assert getattr(app.state, "wrapper_http", None) is None
    client = get_client(app)
    assert get_client(app) is client
    assert await forward(app, target_for(), "GET", "/x") == {"hello": "wrapper"}
    assert seen[0].url.path == "/v1/x"
    await client.aclose()


@pytest.mark.parametrize("value", ["term_0123456789ab", "term_" + "f" * 12, "term_000000000000"])
def test_terminal_id_valid(value):
    assert validate_terminal_id(value) == value
    assert TERMINAL_ID_RE.pattern == r"^term_[0-9a-f]{12}$"


def test_terminal_id_validator():
    bad = [
        "..",
        ".",
        "",
        "term_zz",
        "term_0123456789AB",
        "TERM_0123456789ab",
        "term_0123456789abc",
        "term_0123456789a",
        "term_0123456789ab\n",
        "term_0123456789ab/../x",
        "../term_0123456789ab",
        "a/b",
        "term_0123456789ab?x=1",
        "term_%2e%2e%2e%2e%2e%2e",
        " term_0123456789ab",
    ]
    for value in bad:
        with pytest.raises(ApiError) as err:
            validate_terminal_id(value)
        assert (err.value.status, err.value.code) == (422, "invalid_request"), repr(value)


async def test_token_never_in_logs_or_responses(caplog):
    token = "SECRET-" + uuid.uuid4().hex
    t = target_for(token=token)

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["authorization"] == f"Bearer {token}"
        p = req.url.path
        if p.endswith("/ok"):
            return httpx.Response(200, json={"fine": True})
        if p.endswith("/echo"):
            # a hostile or buggy wrapper that reflects the bearer back
            return httpx.Response(
                400, json={"error": {"code": "echo", "message": f"bad bearer {token}"}}
            )
        if p.endswith("/boom"):
            return httpx.Response(500, text=f"crash {token}")
        raise httpx.ConnectError(f"refused {token}")

    app = app_with(handler)
    bodies: list[str] = []
    caplog.set_level(logging.DEBUG)
    for method, path in (
        ("GET", "/files/tree"),
        ("GET", "/files/ok"),
        ("POST", "/sessions/ses_0/terminals"),
        ("GET", "/sessions/ses_0/terminals/term_0123456789ab/echo"),
        ("DELETE", "/sessions/ses_0/terminals/term_0123456789ab/boom"),
        ("GET", "/sessions/ses_0/terminals/term_0123456789ab/ok"),
    ):
        try:
            bodies.append(repr(await forward(app, t, method, path, params={"p": "x"})))
        except ApiError as exc:
            bodies.append(f"{exc.status} {exc.code} {exc.message}")
            assert token not in repr(exc) and token not in str(exc.__cause__ or "")
    assert len(bodies) == 6
    assert any("fine" in b for b in bodies) and any("wrapper_unreachable" in b for b in bodies)
    assert all(token not in b for b in bodies)
    for rec in caplog.records:
        assert token not in rec.getMessage(), rec.name
        assert token not in str(rec.args) and token not in str(rec.__dict__.get("exc_info"))
    assert caplog.records, "the helper logs at DEBUG"
    assert workspace.log.name.startswith("agents_orchestrator")
