"""Cache key resolution at session create and cache availability in /meta (design D7, D8)."""

from __future__ import annotations

import re
import uuid

from test_api_sessions import assert_error, client_for, make_settings

from agents_orchestrator.api.app import create_app
from agents_orchestrator.spec import CACHE_KEY_RE, default_cache_key

C = "/api/v1/sessions"
P = "/api/v1/presets"


def app_client(pool, dsn, **kw):
    kw.setdefault("cache_s3_bucket", "b")
    return client_for(create_app(make_settings(dsn, **kw), pool))


async def make_preset(http, name, spec):
    r = await http.post(P, json={"name": name, "spec": spec})
    assert r.status_code == 201, r.text
    return r.json()


def test_default_cache_key():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert default_cache_key(a) == default_cache_key(a)
    assert default_cache_key(a) != default_cache_key(b)
    key = default_cache_key(a)
    assert re.fullmatch(r"preset-[0-9a-f]{16}", key)
    assert CACHE_KEY_RE.match(key)


async def test_create_from_preset_derives_key(pool, dsn):
    async with app_client(pool, dsn) as http:
        preset = await make_preset(http, "keyless", {"cache": {}})
        want = default_cache_key(uuid.UUID(preset["id"]))
        keys = []
        for _ in range(2):
            r = await http.post(C, json={"preset_id": preset["id"]})
            assert r.status_code == 201, r.text
            keys.append(r.json()["spec"]["cache"]["key"])
        assert keys == [want, want]
        got = (await http.get(f"{C}/{r.json()['id']}")).json()
        assert got["spec"]["cache"]["key"] == want


async def test_create_from_preset_keeps_user_key(pool, dsn):
    async with app_client(pool, dsn) as http:
        preset = await make_preset(http, "keyed", {"cache": {"key": "mine", "exclude": ["tmp"]}})
        r = await http.post(C, json={"preset_id": preset["id"]})
        assert r.status_code == 201, r.text
        assert r.json()["spec"]["cache"] == {"key": "mine", "exclude": ["tmp"]}


async def test_raw_spec_needs_key(pool, dsn):
    async with app_client(pool, dsn) as http:
        r = await http.post(C, json={"slug": "raw", "spec": {"cache": {}}})
        assert_error(r, 422, "invalid_request")
        assert "cache.key is required without a preset" in r.json()["error"]["message"]
        ok = await http.post(C, json={"slug": "raw2", "spec": {"cache": {"key": "k1"}}})
        assert ok.status_code == 201, ok.text


async def test_preset_cache_stored_sparse(pool, dsn):
    async with app_client(pool, dsn) as http:
        preset = await make_preset(http, "sparse", {"cache": {}})
        assert not preset["spec"]["cache"].get("key")
        put = await http.put(f"{P}/{preset['id']}", json={"name": "sparse", "spec": {"cache": {}}})
        assert put.status_code == 200, put.text
        got = (await http.get(f"{P}/{preset['id']}")).json()
        assert not got["spec"]["cache"].get("key")


async def test_meta_cache(pool, dsn):
    async with app_client(pool, dsn, cache_max_bytes=1234) as http:
        r = await http.get("/api/v1/meta")
        assert r.json()["cache"] == {"available": True, "max_bytes": 1234}
    async with app_client(pool, dsn, cache_s3_bucket="") as http:
        assert (await http.get("/api/v1/meta")).json()["cache"]["available"] is False


def test_default_cache_key_is_pinned():
    # Changing the salt or slice would orphan every existing cache in S3.
    assert default_cache_key(uuid.UUID(int=1)) == "preset-4660fe5f5bc805e5"
