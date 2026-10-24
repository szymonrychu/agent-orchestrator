"""File proxy routes: GET /api/v1/clusters/{id}/files and .../files/content (D1, D4)."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest_asyncio
from factories import insert_agent, insert_cluster, set_agent_status
from fakes.fake_wrapper import FakeWrapper
from test_api_clusters import assert_error, client_for, make_settings

from agent_orchestrator.api import workspace
from agent_orchestrator.api.app import create_app
from agent_orchestrator.tokens import derive_tokens

KEY = "k" * 32


def token_of(cluster_id) -> str:
    return derive_tokens(KEY.encode(), cluster_id).auth


@dataclass
class Rig:
    http: object
    pool: object
    cluster: dict
    fakes: list[FakeWrapper]

    async def agent(self, ordinal: int, *, ready: bool = True, cluster: dict | None = None):
        """A fake wrapper with one session, registered as agent `ordinal` of the cluster."""
        cl = cluster or self.cluster
        fake = FakeWrapper(token=token_of(cl["id"]))
        await fake.start()
        self.fakes.append(fake)
        async with httpx.AsyncClient(base_url=fake.base_url) as direct:
            made = await direct.post(
                "/v1/sessions",
                json={"name": "files"},
                headers={"Authorization": f"Bearer {fake.token}"},
            )
        sid = made.json()["session_id"]
        row = await insert_agent(
            self.pool, cl["id"], ordinal=ordinal, url=fake.base_url, session_id=sid, ready=ready
        )
        await set_agent_status(self.pool, row["id"], state="idle")
        return fake, sid, row

    def url(self, tail: str = "files", cluster: dict | None = None) -> str:
        return f"/api/v1/clusters/{(cluster or self.cluster)['id']}/{tail}"


@pytest_asyncio.fixture
async def rig(pool, dsn) -> AsyncIterator[Rig]:
    app = create_app(make_settings(dsn, master_key=KEY), pool)
    cluster = await insert_cluster(pool)
    state = Rig(None, pool, cluster, [])
    async with client_for(app) as http:
        state.http = http
        try:
            yield state
        finally:
            await workspace.close_client(app)
            for fake in state.fakes:
                await fake.stop()


def put(fake: FakeWrapper, sid: str, rel: str, data: str | bytes = "x") -> None:
    path = fake.workdir(sid) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


async def test_list_proxied_from_first_ready_agent(rig):
    await rig.agent(0, ready=False)  # never ready: skipped
    fake, sid, row = await rig.agent(1)
    other, osid, _ = await rig.agent(2)
    put(fake, sid, "src/main.py", "print('hi')\n")
    put(fake, sid, "README.md", "# demo\n")
    put(fake, sid, ".git/HEAD", "ref: refs/heads/main\n")
    put(other, osid, "only-on-the-other.txt")

    r = await rig.http.get(rig.url())
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"cluster_id", "agent_id", "root", "path", "entries", "next_offset"}
    assert body["cluster_id"] == str(rig.cluster["id"])
    assert body["agent_id"] == str(row["id"])
    assert body["root"] == fake.sessions[sid].workdir
    assert body["root"] != str(fake.workdir(sid))  # reported (logical) vs backing directory
    assert body["path"] == "" and body["next_offset"] is None
    assert [(e["name"], e["type"]) for e in body["entries"]] == [
        (".git", "dir"),
        ("src", "dir"),
        ("README.md", "file"),
    ]
    readme = body["entries"][2]
    assert set(readme) == {"name", "type", "size", "mtime"}
    assert readme["size"] == len("# demo\n") and readme["mtime"]
    assert body["entries"][1]["size"] is None

    sub = (await rig.http.get(rig.url(), params={"path": "src"})).json()
    assert sub["path"] == "src"
    assert [e["name"] for e in sub["entries"]] == ["main.py"]

    # paging passes through intact
    fake.files_max_entries = 2
    first = (await rig.http.get(rig.url())).json()
    assert [e["name"] for e in first["entries"]] == [".git", "src"]
    assert first["next_offset"] == 2
    second = (await rig.http.get(rig.url(), params={"offset": first["next_offset"]})).json()
    assert [e["name"] for e in second["entries"]] == ["README.md"]
    assert second["next_offset"] is None
    bad = await rig.http.get(rig.url(), params={"offset": -1})
    assert_error(bad, 422, "invalid_request")


async def test_agent_id_selects_and_scopes(rig):
    first, fsid, _ = await rig.agent(0)
    second, ssid, row2 = await rig.agent(1)
    put(first, fsid, "a.txt", "first")
    put(second, ssid, "b.txt", "second")

    body = (await rig.http.get(rig.url(), params={"agent_id": str(row2["id"])})).json()
    assert body["agent_id"] == str(row2["id"])
    assert [e["name"] for e in body["entries"]] == ["b.txt"]
    got = (
        await rig.http.get(
            rig.url("files/content"), params={"agent_id": str(row2["id"]), "path": "b.txt"}
        )
    ).json()
    assert got["agent_id"] == str(row2["id"]) and got["content"] == "second"

    # an agent of another cluster, or an unknown one, is not_found; so is an unknown cluster
    foreign_cluster = await insert_cluster(rig.pool)
    _, _, foreign = await rig.agent(0, cluster=foreign_cluster)
    for route in ("files", "files/content"):
        r = await rig.http.get(rig.url(route), params={"agent_id": str(foreign["id"])})
        assert_error(r, 404, "not_found")
        r = await rig.http.get(rig.url(route), params={"agent_id": str(uuid.uuid4())})
        assert_error(r, 404, "not_found")
        r = await rig.http.get(f"/api/v1/clusters/{uuid.uuid4()}/{route}")
        assert_error(r, 404, "not_found")


async def test_content_proxied(rig):
    fake, sid, row = await rig.agent(0)
    put(fake, sid, "src/main.py", "print('hé')\n")
    put(fake, sid, "blob.bin", b"\x89PNG\x00\x01")
    put(fake, sid, "latin.txt", b"caf\xe9")
    put(fake, sid, "big.log", "x" * 64)
    fake.file_preview_max_bytes = 32

    r = await rig.http.get(rig.url("files/content"), params={"path": "src/main.py"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "cluster_id",
        "agent_id",
        "path",
        "size",
        "mtime",
        "mime",
        "kind",
        "content",
    }
    assert body["cluster_id"] == str(rig.cluster["id"]) and body["agent_id"] == str(row["id"])
    assert body["path"] == "src/main.py"
    assert body["kind"] == "text" and body["content"] == "print('hé')\n"
    assert body["mime"] == "text/x-python"
    assert body["size"] == len("print('hé')\n".encode())

    for name in ("blob.bin", "latin.txt"):
        b = (await rig.http.get(rig.url("files/content"), params={"path": name})).json()
        assert b["kind"] == "binary" and b["content"] is None, name
    big = (await rig.http.get(rig.url("files/content"), params={"path": "big.log"})).json()
    assert big["kind"] == "too_large" and big["content"] is None and big["size"] == 64


async def test_errors_pass_through(rig):
    fake, sid, _ = await rig.agent(0)
    put(fake, sid, "src/main.py")
    for params, status, code in (
        ({"path": "../etc"}, 400, "invalid_path"),
        ({"path": "/etc"}, 400, "invalid_path"),
        ({"path": "a\x00b"}, 400, "invalid_path"),
        ({"path": "nope"}, 404, "not_found"),
        ({"path": "src/main.py"}, 400, "not_a_directory"),
    ):
        r = await rig.http.get(rig.url(), params=params)
        assert_error(r, status, code)
    for params, status, code in (
        ({"path": "../etc/passwd"}, 400, "invalid_path"),
        ({"path": "nope.txt"}, 404, "not_found"),
        ({"path": "src"}, 400, "not_a_file"),
    ):
        r = await rig.http.get(rig.url("files/content"), params=params)
        assert_error(r, status, code)

    # a wrapper that rejects the derived bearer is relayed, never retried with another
    fake.token = "different"
    r = await rig.http.get(rig.url())
    assert r.status_code == 401
    assert token_of(rig.cluster["id"]) not in r.text
    fake.token = token_of(rig.cluster["id"])

    await fake.set_unreachable(True)
    for route in ("files", "files/content"):
        r = await rig.http.get(rig.url(route), params={"path": "src"})
        assert_error(r, 502, "wrapper_unreachable")
        assert token_of(rig.cluster["id"]) not in r.text
    await fake.set_unreachable(False)


async def test_no_ready_agent(rig):
    for route in ("files", "files/content"):
        assert_error(await rig.http.get(rig.url(route)), 409, "agent_not_ready")
    await rig.agent(0, ready=False)
    for route in ("files", "files/content"):
        assert_error(await rig.http.get(rig.url(route)), 409, "agent_not_ready")


async def test_missing_root_not_found_passes_through(rig):
    fake, sid, _ = await rig.agent(0)
    put(fake, sid, "a.txt")
    assert (await rig.http.get(rig.url())).status_code == 200
    shutil.rmtree(fake.workdir(sid))
    r = await rig.http.get(rig.url(), params={"path": ""})
    assert_error(r, 404, "not_found")
    r = await rig.http.get(rig.url())
    assert_error(r, 404, "not_found")
