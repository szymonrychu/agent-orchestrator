"""File download proxy route: GET /api/v1/sessions/{id}/files/download (D4, D7).

Docker-free: a real `FakeWrapper`, `workspace.resolve_agent` monkeypatched to return a
`WrapperTarget` for it (or to raise `ApiError`), and a stub pool whose `connection()` is an
async context manager. Agent choice and scoping against the database live in
tests/test_api_files.py (CI only).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest
import pytest_asyncio
from fakes.fake_wrapper import FakeWrapper
from starlette.requests import ClientDisconnect
from test_api_sessions import PEER, assert_error, client_for, make_settings

from agents_orchestrator.api import workspace
from agents_orchestrator.api.app import create_app
from agents_orchestrator.api.errors import ApiError
from agents_orchestrator.tokens import derive_tokens

KEY = "k" * 32
SESSION = uuid.uuid4()
AGENT = uuid.uuid4()
TOKEN = derive_tokens(KEY.encode(), SESSION).auth
DOWNLOAD_HEADERS = ("content-type", "content-length", "content-disposition")


class StubPool:
    """Stands in for the database pool: `resolve_agent` is patched, so nothing is queried."""

    @contextlib.asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncIterator[object]:
        yield object()


@dataclass
class Rig:
    app: object
    http: httpx.AsyncClient
    fake: FakeWrapper
    sid: str
    refusal: ApiError | None = None
    resolved: list[tuple[uuid.UUID | None, uuid.UUID | None]] = field(default_factory=list)

    def url(self, tail: str = "files/download") -> str:
        return f"/api/v1/sessions/{SESSION}/{tail}"

    def put(self, rel: str, data: bytes) -> None:
        path = self.fake.workdir(self.sid) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def direct(self, rel: str) -> httpx.Response:
        """The fake wrapper's own answer for `rel`, read in full."""
        async with httpx.AsyncClient(base_url=self.fake.base_url) as c:
            return await c.get(
                f"/v1/sessions/{self.sid}/files/download",
                params={"path": rel},
                headers={"Authorization": f"Bearer {self.fake.token}"},
            )


@pytest_asyncio.fixture
async def rig(monkeypatch) -> AsyncIterator[Rig]:
    fake = FakeWrapper(token=TOKEN)
    await fake.start()
    async with httpx.AsyncClient(base_url=fake.base_url) as direct:
        made = await direct.post(
            "/v1/sessions", json={"name": "files"}, headers={"Authorization": f"Bearer {TOKEN}"}
        )
    sid = made.json()["session_id"]
    fake.requests.clear()
    target = workspace.WrapperTarget(
        agent_id=AGENT,
        session_id=SESSION,
        pod_name="agent-files-0",
        url=fake.base_url,
        wrapper_session_id=sid,
        token=TOKEN,
    )
    app = create_app(make_settings(master_key=KEY), pool=StubPool())
    async with client_for(app) as http:
        state = Rig(app, http, fake, sid)

        async def resolve(conn, settings, *, session_id=None, agent_id=None):
            state.resolved.append((session_id, agent_id))
            if state.refusal is not None:
                raise state.refusal
            return target

        monkeypatch.setattr(workspace, "resolve_agent", resolve)
        try:
            yield state
        finally:
            await workspace.close_client(app)
            await fake.stop()


@pytest.fixture
def closed(monkeypatch) -> list[httpx.Response]:
    """Every upstream (fake wrapper) response whose `aclose` ran, once each, in call order.

    httpx closes a response itself when `aiter_raw` is exhausted, so a call may repeat."""
    seen: list[httpx.Response] = []
    original = httpx.Response.aclose

    async def spy(self: httpx.Response) -> None:
        if self.request.url.host == "127.0.0.1" and not any(s is self for s in seen):
            seen.append(self)
        await original(self)

    monkeypatch.setattr(httpx.Response, "aclose", spy)
    return seen


async def test_download_streams_bytes_exact(rig):
    text = "line one\nzażółć\n".encode()
    binary = bytes(range(256)) * 16 + b"\x00\xff\x1f\x8b"
    big = os.urandom(3 * 1024 * 1024 + 1)
    rig.put("notes.txt", text)
    rig.put("bin/zdjęcie.bin", binary)
    rig.put("big.dat", big)

    for rel, data in (("notes.txt", text), ("bin/zdjęcie.bin", binary), ("big.dat", big)):
        r = await rig.http.get(rig.url(), params={"path": rel})
        assert r.status_code == 200, r.text[:200]
        assert r.content == data, rel
        upstream = await rig.direct(rel)
        assert upstream.content == data
        for name in DOWNLOAD_HEADERS:
            assert r.headers[name] == upstream.headers[name], (rel, name)
        assert r.headers["content-length"] == str(len(data))
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["cache-control"] == "no-store"
        assert TOKEN not in str(r.headers)

    r = await rig.http.get(rig.url(), params={"path": "notes.txt"})
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers["content-disposition"] == (
        "attachment; filename=\"notes.txt\"; filename*=UTF-8''notes.txt"
    )
    r = await rig.http.get(rig.url(), params={"path": "bin/zdjęcie.bin"})
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["content-disposition"] == (
        "attachment; filename=\"zdj_cie.bin\"; filename*=UTF-8''zdj%C4%99cie.bin"
    )
    # the route asked the default agent choice, then the wrapper's own session route
    assert rig.resolved[0] == (SESSION, None)
    assert ("GET", f"/v1/sessions/{rig.sid}/files/download") in rig.fake.requests

    pinned = uuid.uuid4()
    r = await rig.http.get(rig.url(), params={"path": "notes.txt", "agent_id": str(pinned)})
    assert r.status_code == 200 and r.content == text
    assert rig.resolved[-1] == (SESSION, pinned)


async def test_download_errors_pass_through(rig):
    rig.put("src/main.py", b"print('hi')\n")
    for path, status, code in (
        ("../etc/passwd", 400, "invalid_path"),
        ("/etc/passwd", 400, "invalid_path"),
        ("src", 400, "not_a_file"),
        ("", 400, "not_a_file"),
        ("nope.txt", 404, "not_found"),
    ):
        r = await rig.http.get(rig.url(), params={"path": path})
        assert_error(r, status, code)
        assert r.headers["content-type"] == "application/json"
        assert "content-disposition" not in r.headers

    # a wrapper that rejects the derived bearer is relayed, never with the bearer in it
    rig.fake.token = "different"
    r = await rig.http.get(rig.url(), params={"path": "src/main.py"})
    assert_error(r, 401, "unauthorized")
    assert TOKEN not in r.text and TOKEN not in str(r.headers)
    rig.fake.token = TOKEN

    await rig.fake.set_unreachable(True)
    r = await rig.http.get(rig.url(), params={"path": "src/main.py"})
    assert_error(r, 502, "wrapper_unreachable")
    assert TOKEN not in r.text
    await rig.fake.set_unreachable(False)

    # a wrapper 5xx is 502 wrapper_error and its body is not relayed
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": "internal", "message": "secret"}})

    await workspace.close_client(rig.app)
    rig.app.state.wrapper_http = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    r = await rig.http.get(rig.url(), params={"path": "src/main.py"})
    assert_error(r, 502, "wrapper_error")
    assert "secret" not in r.text


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "not_found"), (409, "agent_not_ready"), (503, "unavailable")],
)
async def test_download_resolve_errors(rig, status, code):
    rig.put("a.txt", b"a")
    rig.refusal = ApiError(status, code, f"refused with {code}")
    r = await rig.http.get(rig.url(), params={"path": "a.txt"})
    assert_error(r, status, code)
    assert r.headers["content-type"] == "application/json"
    assert rig.resolved == [(SESSION, None)]
    assert rig.fake.requests == []  # refused before any wrapper call


def _scope(rig: Rig, rel: str, spec_version: str) -> dict:
    """An ASGI http scope for the download of `rel`, as a server would hand it over."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": rig.url(),
        "raw_path": rig.url().encode(),
        "query_string": f"path={rel}".encode(),
        "root_path": "",
        "headers": [(b"host", b"orch")],
        "client": PEER,
        "server": ("orch", 80),
    }


async def _leave_after_first_chunk(rig: Rig, rel: str, spec_version: str) -> list[dict]:
    """Drive the app directly and disconnect once the first body chunk was sent.

    ASGI 2.4 servers report it as an `OSError` from `send`; older ones as `http.disconnect`
    from `receive`. Returns every message the app sent.
    """
    sent: list[dict] = []
    first_body = asyncio.Event()

    async def receive() -> dict:
        await first_body.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)
        if message["type"] == "http.response.body":
            first_body.set()
            if spec_version == "2.4":
                raise OSError("browser went away")

    with contextlib.suppress(ClientDisconnect):
        await asyncio.wait_for(rig.app(_scope(rig, rel, spec_version), receive, send), 10)
    return sent


async def test_download_closes_upstream(rig, closed):
    rig.put("a.bin", b"\x00" * 4096)

    r = await rig.http.get(rig.url(), params={"path": "a.bin"})
    assert r.status_code == 200 and len(r.content) == 4096
    assert len(closed) == 1 and closed[0].is_closed
    assert closed[0].request.url.path == f"/v1/sessions/{rig.sid}/files/download"

    r = await rig.http.get(rig.url(), params={"path": "missing.bin"})
    assert_error(r, 404, "not_found")
    assert len(closed) == 2 and closed[1].is_closed

    # the browser goes away mid-download: both ASGI disconnect styles close the upstream
    rig.put("big.bin", os.urandom(3 * 1024 * 1024))
    for spec_version in ("2.0", "2.4"):
        before = len(closed)
        sent = await _leave_after_first_chunk(rig, "big.bin", spec_version)
        assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
        assert not any(m.get("more_body") is False for m in sent), spec_version
        assert len(closed) == before + 1 and closed[-1].is_closed, spec_version


async def test_listing_target_type_passes_through(rig):
    rig.put("docs/a.txt", b"a")
    workdir = rig.fake.workdir(rig.sid)
    os.symlink("docs/a.txt", workdir / "to-file")
    os.symlink("docs", workdir / "to-dir")
    os.symlink("/etc/passwd", workdir / "outside")
    os.symlink("missing", workdir / "dangling")

    r = await rig.http.get(rig.url("files"))
    assert r.status_code == 200, r.text
    entries = {e["name"]: e for e in r.json()["entries"]}
    assert {name: (e["type"], e["target_type"]) for name, e in entries.items()} == {
        "docs": ("dir", None),
        "to-file": ("symlink", "file"),
        "to-dir": ("symlink", "dir"),
        "outside": ("symlink", None),
        "dangling": ("symlink", None),
    }
    for e in entries.values():
        assert set(e) == {"name", "type", "size", "mtime", "target_type"}

    sub = (await rig.http.get(rig.url("files"), params={"path": "docs"})).json()
    assert sub["entries"] == [
        {
            "name": "a.txt",
            "type": "file",
            "size": 1,
            "mtime": sub["entries"][0]["mtime"],
            "target_type": None,
        }
    ]


class _FailingStream(httpx.AsyncByteStream):
    """Yields `head`, then fails like a wrapper connection dropped mid-body."""

    def __init__(self, head: bytes) -> None:
        self.head = head
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self.head
        raise httpx.ReadError("connection reset by peer")

    async def aclose(self) -> None:
        self.closed = True


async def test_download_midstream_failure_aborts(rig, closed):
    head = b"\x00partial" * 1024
    stream = _FailingStream(head)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=stream,
            headers={
                "content-type": "application/octet-stream",
                "content-length": str(len(head) * 4),
                "content-disposition": "attachment; filename=\"a.bin\"; filename*=UTF-8''a.bin",
            },
        )

    rig.app.state.wrapper_http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(workspace.DownloadAborted):
        await rig.http.get(rig.url(), params={"path": "a.bin"})
    assert stream.closed
    assert len(closed) == 1 and closed[0].is_closed

    # driven as a server would: the response starts, then never completes
    sent: list[dict] = []

    async def receive() -> dict:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict) -> None:
        sent.append(message)

    stream.closed = False
    with pytest.raises(workspace.DownloadAborted):
        await rig.app(_scope(rig, "a.bin", "2.4"), receive, send)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 200
    bodies = [m for m in sent if m["type"] == "http.response.body"]
    assert b"".join(m["body"] for m in bodies) == head
    assert all(m.get("more_body") for m in bodies)  # never a clean end of body
    assert stream.closed and len(closed) == 2 and closed[1].is_closed
    assert TOKEN not in str(sent)
