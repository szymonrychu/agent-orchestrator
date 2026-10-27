"""File write proxy route: PUT /api/v1/sessions/{id}/files/content (D5).

Docker-free, on the download tests' rig (tests/test_api_file_download.py): a real
`FakeWrapper`, `workspace.resolve_agent` monkeypatched to return a `WrapperTarget` for it (or
to raise `ApiError`), and a stub pool. Agent choice against the database lives in
tests/test_api_files.py (CI only).
"""

from __future__ import annotations

import logging
import uuid

import httpx
import pytest
from test_api_file_download import AGENT, SESSION, TOKEN, Rig
from test_api_file_download import rig as rig  # the shared fixture
from test_api_sessions import assert_error

from agents_orchestrator.api import schemas, workspace
from agents_orchestrator.api.errors import ApiError

CONTENT = "def main():\r\n    print('zażółć')\n\tpass\n"
WRITTEN_KEYS = {"session_id", "agent_id", "path", "size", "mtime"}
UI_ORIGIN = "http://ui.example:5173"


def content_url(rig: Rig) -> str:
    return rig.url("files/content")


async def stamp(rig: Rig, rel: str) -> dict:
    """The `{mtime, size}` the editor loaded for `rel` (the content route's answer)."""
    r = await rig.http.get(content_url(rig), params={"path": rel})
    assert r.status_code == 200, r.text
    return {"mtime": r.json()["mtime"], "size": r.json()["size"]}


async def write(rig: Rig, rel: str, content: str, expected, **kw) -> httpx.Response:
    return await rig.http.put(
        content_url(rig),
        params={"path": rel, **kw.pop("params", {})},
        json={"content": content, "expected": expected},
        **kw,
    )


def on_disk(rig: Rig, rel: str) -> bytes:
    return (rig.fake.workdir(rig.sid) / rel).read_bytes()


async def test_write_proxied(rig):
    rig.put("src/main.py", b"print('old')\n")
    loaded = await stamp(rig, "src/main.py")
    rig.fake.requests.clear()

    r = await write(rig, "src/main.py", CONTENT, loaded)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == WRITTEN_KEYS
    assert body["session_id"] == str(SESSION) and body["agent_id"] == str(AGENT)
    assert body["path"] == "src/main.py"
    assert body["size"] == len(CONTENT.encode())
    assert on_disk(rig, "src/main.py") == CONTENT.encode()  # byte-exact, CRLF kept
    assert await stamp(rig, "src/main.py") == {"mtime": body["mtime"], "size": body["size"]}
    assert rig.fake.requests[0] == ("PUT", f"/v1/sessions/{rig.sid}/files/content")
    assert rig.resolved[0] == (SESSION, None)

    # the answer is the next save's baseline; an agent_id reaches the agent choice
    pinned = uuid.uuid4()
    again = {"mtime": body["mtime"], "size": body["size"]}
    r = await write(rig, "src/main.py", "x = 1\n", again, params={"agent_id": str(pinned)})
    assert r.status_code == 200, r.text
    assert on_disk(rig, "src/main.py") == b"x = 1\n"
    assert rig.resolved[-1] == (SESSION, pinned)


async def test_write_errors_pass_through(rig):
    rig.put("src/main.py", b"print('hi')\n")
    loaded = await stamp(rig, "src/main.py")

    stale = {**loaded, "size": loaded["size"] + 1}
    r = await write(rig, "src/main.py", "lost\n", stale)
    assert_error(r, 409, "file_changed")
    assert on_disk(rig, "src/main.py") == b"print('hi')\n"

    for path, status, code in (
        ("../etc/passwd", 400, "invalid_path"),
        ("/etc/passwd", 400, "invalid_path"),
        ("src", 400, "not_a_file"),
        ("", 400, "not_a_file"),
        ("nope.txt", 404, "not_found"),
    ):
        assert_error(await write(rig, path, "x", None), status, code)
    assert not (rig.fake.workdir(rig.sid) / "nope.txt").exists()

    rig.fake.file_preview_max_bytes = 8
    assert_error(await write(rig, "src/main.py", "x" * 9, None), 413, "too_large")
    assert on_disk(rig, "src/main.py") == b"print('hi')\n"
    rig.fake.file_preview_max_bytes = 1048576

    # the agent changed the file meanwhile: `expected: null` overwrites anyway
    rig.put("src/main.py", b"agent edit, longer than before\n")
    r = await write(rig, "src/main.py", "mine\n", None)
    assert r.status_code == 200, r.text
    assert on_disk(rig, "src/main.py") == b"mine\n"

    # a wrapper that rejects the derived bearer is relayed, never with the bearer in it
    rig.fake.token = "different"
    r = await write(rig, "src/main.py", "x", None)
    assert_error(r, 401, "unauthorized")
    assert TOKEN not in r.text and TOKEN not in str(r.headers)
    rig.fake.token = TOKEN

    await rig.fake.set_unreachable(True)
    r = await write(rig, "src/main.py", "x", None)
    assert_error(r, 502, "wrapper_unreachable")
    assert TOKEN not in r.text
    await rig.fake.set_unreachable(False)

    # a wrapper 5xx is 502 wrapper_error and its body is not relayed
    def failing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"code": "internal", "message": "secret"}})

    await workspace.close_client(rig.app)
    rig.app.state.wrapper_http = httpx.AsyncClient(transport=httpx.MockTransport(failing))
    r = await write(rig, "src/main.py", "x", None)
    assert_error(r, 502, "wrapper_error")
    assert "secret" not in r.text


@pytest.mark.parametrize(
    "body",
    [
        {"content": "x", "expected": None, "mode": "rw"},  # unknown key
        {"expected": None},  # no content
        {"content": "x"},  # no expected
        {"content": "x", "expected": {"mtime": "2026-10-27T09:00:00.000Z", "size": -1}},
        {"content": "x", "expected": {"mtime": "2026-10-27T09:00:00.000Z", "size": 1, "a": 1}},
        {"content": "x", "expected": {"size": 1}},
        {"content": 1, "expected": None},
        "not an object",
    ],
)
async def test_write_body_validated(rig, body):
    rig.put("a.txt", b"a")
    r = await rig.http.put(content_url(rig), params={"path": "a.txt"}, json=body)
    assert_error(r, 422, "invalid_request")
    assert rig.resolved == [] and rig.fake.requests == []  # refused before any wrapper call
    assert on_disk(rig, "a.txt") == b"a"


async def test_write_body_lone_surrogate_validated(rig):
    """A lone surrogate is no text a file can hold: refused like the wrapper does, never a 500."""
    rig.put("a.txt", b"a")
    raw = b'{"content": "a\\ud800b", "expected": null}'
    r = await rig.http.put(
        content_url(rig),
        params={"path": "a.txt"},
        content=raw,
        headers={"content-type": "application/json"},
    )
    assert_error(r, 422, "invalid_request")
    assert rig.resolved == [] and rig.fake.requests == []


WRAPPER_CAP = 1048576  # the wrapper's default `limits.file_preview_max_bytes`
ESCAPED_X = "\\u0078"  # six bytes of JSON for one byte of content: the worst case


def escaped_body(count: int, tail: str = "") -> bytes:
    """A JSON write body whose `content` is `count` bytes of "x", every one written as \\u0078."""
    return ('{"content": "' + ESCAPED_X * count + tail + '", "expected": null}').encode()


async def put_raw(rig: Rig, body, **headers) -> httpx.Response:
    return await rig.http.put(
        content_url(rig),
        params={"path": "a.txt"},
        content=body,
        headers={"content-type": "application/json", **headers},
    )


async def chunks(body: bytes, size: int = 65536):
    for i in range(0, len(body), size):
        yield body[i : i + size]


async def test_write_body_too_large(rig):
    """A body no cap-sized content can produce is 413 before it is parsed or forwarded."""
    rig.put("a.txt", b"a")
    body = escaped_body(WRAPPER_CAP)
    body += b" " * (schemas.WRITE_BODY_MAX_BYTES + 1 - len(body))  # the smallest refused body

    # declared up front (Content-Length): refused without reading the body
    r = await put_raw(rig, body)
    assert_error(r, 413, "too_large")
    # chunked, no Content-Length: refused as soon as the stream passes the cap
    r = await put_raw(rig, chunks(body))
    assert_error(r, 413, "too_large")
    # a plain oversized content too
    r = await write(rig, "a.txt", "x" * schemas.WRITE_BODY_MAX_BYTES, None)
    assert_error(r, 413, "too_large")

    assert rig.resolved == [] and rig.fake.requests == []  # before any wrapper request
    assert on_disk(rig, "a.txt") == b"a"


async def test_write_at_cap_forwarded(rig):
    """Content at the wrapper's cap is forwarded, plain or with every byte escaped (6x)."""
    rig.put("a.txt", b"a")
    loaded = await stamp(rig, "a.txt")
    rig.fake.requests.clear()

    full = "x" * WRAPPER_CAP
    r = await write(rig, "a.txt", full, loaded)
    assert r.status_code == 200, r.text
    assert r.json()["size"] == WRAPPER_CAP
    assert on_disk(rig, "a.txt") == full.encode()
    assert rig.fake.requests == [("PUT", f"/v1/sessions/{rig.sid}/files/content")]

    escaped = escaped_body(WRAPPER_CAP)
    assert 6 * WRAPPER_CAP < len(escaped) <= schemas.WRITE_BODY_MAX_BYTES
    for payload in (escaped, chunks(escaped)):
        rig.put("a.txt", b"a")
        r = await put_raw(rig, payload)
        assert r.status_code == 200, r.text
        assert r.json()["size"] == WRAPPER_CAP

    # one byte over the wrapper's cap is the wrapper's own 413, relayed
    r = await write(rig, "a.txt", "x" * (WRAPPER_CAP + 1), None)
    assert_error(r, 413, "too_large")
    assert on_disk(rig, "a.txt") == full.encode()


async def test_write_origin_guarded(rig):
    rig.put("a.txt", b"a")
    settings = rig.app.state.settings
    rig.app.state.settings = settings.model_copy(update={"allowed_origins": [UI_ORIGIN]})

    for origin in ("http://evil.example", "null", "http://ui.example:5174"):
        r = await write(rig, "a.txt", "x", None, headers={"Origin": origin})
        assert_error(r, 403, "forbidden_origin")
    # the guard runs before the body is even validated
    r = await rig.http.put(
        content_url(rig),
        params={"path": "a.txt"},
        json={},
        headers={"Origin": "http://evil.example"},
    )
    assert_error(r, 403, "forbidden_origin")
    assert rig.resolved == [] and rig.fake.requests == []
    assert on_disk(rig, "a.txt") == b"a"

    r = await write(rig, "a.txt", "allowed\n", None, headers={"Origin": UI_ORIGIN})
    assert r.status_code == 200, r.text
    assert on_disk(rig, "a.txt") == b"allowed\n"
    r = await write(rig, "a.txt", "no origin\n", None)  # a non-browser client
    assert r.status_code == 200, r.text
    assert on_disk(rig, "a.txt") == b"no origin\n"


@pytest.mark.parametrize(
    ("status", "code"),
    [(404, "not_found"), (409, "agent_not_ready"), (503, "unavailable")],
)
async def test_write_resolve_errors(rig, status, code):
    rig.put("a.txt", b"a")
    rig.refusal = ApiError(status, code, f"refused with {code}")
    r = await write(rig, "a.txt", "x", None)
    assert_error(r, status, code)
    assert r.headers["content-type"] == "application/json"
    assert rig.resolved == [(SESSION, None)]
    assert rig.fake.requests == []  # refused before any wrapper call
    assert on_disk(rig, "a.txt") == b"a"


async def test_write_never_logs_content_or_token(rig, caplog):
    secret = "TOP-SECRET-FILE-BODY-" + uuid.uuid4().hex
    rig.put("a.txt", b"a")
    loaded = await stamp(rig, "a.txt")
    caplog.set_level(logging.DEBUG)

    assert (await write(rig, "a.txt", secret, loaded)).status_code == 200
    assert_error(await write(rig, "a.txt", secret, loaded), 409, "file_changed")
    rig.fake.file_preview_max_bytes = 8
    assert_error(await write(rig, "a.txt", secret, None), 413, "too_large")
    rig.fake.token = "different"
    assert_error(await write(rig, "a.txt", secret, None), 401, "unauthorized")

    ours = [r for r in caplog.records if r.name == workspace.log.name]
    assert any("PUT" in r.getMessage() for r in ours)  # the forward debug lines did run
    for record in caplog.records:
        text = f"{record.getMessage()} {record.__dict__}"
        assert secret not in text, record.name
        assert TOKEN not in text, record.name


async def test_reads_still_proxied(rig):
    rig.put("src/main.py", b"print('hi')\n")

    listing = await rig.http.get(rig.url("files"), params={"path": "src"})
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["session_id"] == str(SESSION) and body["agent_id"] == str(AGENT)
    assert [e["name"] for e in body["entries"]] == ["main.py"]

    got = await rig.http.get(content_url(rig), params={"path": "src/main.py"})
    assert got.status_code == 200, got.text
    content = got.json()
    assert content["session_id"] == str(SESSION) and content["agent_id"] == str(AGENT)
    assert content["kind"] == "text" and content["content"] == "print('hi')\n"

    assert rig.fake.requests == [
        ("GET", f"/v1/sessions/{rig.sid}/files"),
        ("GET", f"/v1/sessions/{rig.sid}/files/content"),
    ]
    assert_error(await rig.http.get(content_url(rig), params={"path": "src"}), 400, "not_a_file")
