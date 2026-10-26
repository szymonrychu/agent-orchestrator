"""spec.any_mcp_sources: remote https plugin sources, derived names, collisions, API refusal."""

from __future__ import annotations

import re

import pytest
import pytest_asyncio
from pydantic import ValidationError
from test_api_sessions import client_for, make_settings

from agents_orchestrator.api.app import create_app
from agents_orchestrator.spec import (
    ANY_MCP_SOURCE_MAX_CHARS,
    ANY_MCP_SOURCES_MAX,
    SessionSpec,
    SpecInvalid,
    source_plugin_name,
    validate_spec,
)

REMOTE = [
    "git::https://example.org/team/tools.git",
    "github.com/acme/Some_Plugin//plugins/Search?ref=v1.2",
    "git::https://gitlab.example.com/a/b//x/y",
]


@pytest_asyncio.fixture
async def http(pool, dsn):
    app = create_app(make_settings(dsn), pool)
    async with client_for(app) as c:
        yield c


def check(spec: SessionSpec, **settings) -> list[str]:
    try:
        validate_spec(spec, make_settings(**settings))
    except SpecInvalid as exc:
        return exc.errors
    return []


async def test_sparse_when_unset(http, pool):
    assert SessionSpec().any_mcp_sources is None
    r = await http.post("/api/v1/presets", json={"name": "p", "spec": {"replicas": 1}})
    assert r.status_code == 201, r.text
    assert "any_mcp_sources" not in r.json()["spec"]
    r = await http.post("/api/v1/sessions", json={"slug": "s1", "spec": {"replicas": 1}})
    assert r.status_code == 201, r.text
    # the view lists every optional field as null; the stored jsonb carries no such key
    assert r.json()["spec"]["any_mcp_sources"] is None
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT spec FROM sessions WHERE slug = 's1'")
        assert "any_mcp_sources" not in (await cur.fetchone())[0]


async def test_remote_sources_accepted(http):
    spec = SessionSpec(any_mcp_sources=REMOTE)
    assert spec.any_mcp_sources == REMOTE
    assert check(spec) == []
    r = await http.post("/api/v1/presets", json={"name": "p", "spec": {"any_mcp_sources": REMOTE}})
    assert r.status_code == 201, r.text
    assert r.json()["spec"]["any_mcp_sources"] == REMOTE
    got = await http.get(f"/api/v1/presets/{r.json()['id']}")
    assert got.json()["spec"]["any_mcp_sources"] == REMOTE


@pytest.mark.parametrize(
    "bad",
    [
        "./local/dir",
        "../up",
        "/abs/path",
        "git::ssh://git@host/team/repo.git",
        "git::file:///srv/repo",
        "git@github.com:acme/repo.git",
        "git::HTTPS://example.org/a/b",
        "GitHub.com/acme/repo",
        "https://example.org/a/b",
        "git::https://example.org",
        "git::https://example.org/",
        "git::https:///a/b",
        "github.com/acme",
        "github.com/acme/repo/extra",
        "git::https://example.org/a/b?ref=x&ref=y",
        "git::https://example.org/a/b?ref=",
        "git::https://example.org/a/b?branch=x",
        "git::https://example.org/a/b//../c",
        "git::https://example.org/a/b //c",
        "github.com/acme/repo\tx",
        "github.com/acme/repo\x00",
        "nonsense",
    ],
)
def test_refused_sources(bad):
    with pytest.raises(ValidationError) as err:
        SessionSpec(any_mcp_sources=[REMOTE[0], bad])
    msg = str(err.value)
    assert "any_mcp_sources[1]" in msg
    assert "any_mcp_sources[0]" not in msg
    assert len(msg.split("any_mcp_sources[1]:", 1)[1].strip()) > 10


def test_ssh_reason_names_https_only():
    with pytest.raises(ValidationError, match=r"any_mcp_sources\[0\].*https"):
        SessionSpec(any_mcp_sources=["git::ssh://git@host/a/b.git"])


def test_caps():
    SessionSpec(any_mcp_sources=[REMOTE[0]] * ANY_MCP_SOURCES_MAX)
    with pytest.raises(ValidationError, match="at most 20"):
        SessionSpec(any_mcp_sources=[REMOTE[0]] * (ANY_MCP_SOURCES_MAX + 1))
    long = "github.com/acme/" + "r" * ANY_MCP_SOURCE_MAX_CHARS
    with pytest.raises(ValidationError, match=r"any_mcp_sources\[0\].*max 500"):
        SessionSpec(any_mcp_sources=[long])
    ok = "github.com/acme/" + "r" * (ANY_MCP_SOURCE_MAX_CHARS - len("github.com/acme/"))
    assert SessionSpec(any_mcp_sources=[ok]).any_mcp_sources == [ok]
    for empty in ("", "   "):
        with pytest.raises(ValidationError, match=r"any_mcp_sources\[1\] must not be empty"):
            SessionSpec(any_mcp_sources=[REMOTE[0], empty])


@pytest.mark.parametrize(
    ("source", "name"),
    [
        ("git::https://example.org/team/tools.git", "tools"),
        ("git::https://example.org/team/Tools", "tools"),
        ("github.com/acme/Some_Plugin", "some_plugin"),
        ("github.com/acme/repo.git//plugins/Search?ref=v1", "search"),
        ("github.com/acme/repo//a/b/C.D", "c-d"),
        ("git::https://example.org/a/" + "x" * 30, "x" * 20),
        ("git::https://example.org/a/repo/", "repo"),
    ],
)
def test_source_plugin_name(source, name):
    assert source_plugin_name(source) == name


def test_name_collisions_refused():
    def sources(*s):
        return SessionSpec(any_mcp_sources=list(s))

    errs = check(sources("github.com/a/ok", "github.com/b/ok"))
    assert len(errs) == 1 and errs[0].startswith("any_mcp_sources[1]:") and "ok" in errs[0]
    errs = check(sources("github.com/a/ok", "github.com/a/orchestrator"))
    assert errs and errs[0].startswith("any_mcp_sources[1]:") and "orchestrator" in errs[0]
    errs = check(sources("github.com/a/ok", "github.com/a/9bad"))
    assert errs and errs[0].startswith("any_mcp_sources[1]:") and "9bad" in errs[0]
    errs = check(sources("github.com/a/.git"))
    assert errs and errs[0].startswith("any_mcp_sources[0]:")
    # spec's own config
    cfg = {"plugins": [{"name": "taken", "plugin": "pkg.mod:Cls"}]}
    errs = check(SessionSpec(any_mcp_sources=["github.com/a/taken"], any_mcp_config=cfg))
    assert errs and errs[0].startswith("any_mcp_sources[0]:") and "taken" in errs[0]
    assert check(SessionSpec(any_mcp_sources=["github.com/a/free"], any_mcp_config=cfg)) == []


def test_name_collides_with_settings_default_config(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("plugins:\n  - name: dflt\n    plugin: pkg.mod:Cls\n")
    spec = SessionSpec(any_mcp_sources=["github.com/a/dflt"])
    errs = check(spec, any_mcp_config_file=str(f))
    assert errs and errs[0].startswith("any_mcp_sources[0]:") and "dflt" in errs[0]
    # the spec's own config replaces the default for this check
    own = SessionSpec(any_mcp_sources=["github.com/a/dflt"], any_mcp_config={})
    assert check(own, any_mcp_config_file=str(f)) == []


@pytest.mark.parametrize(
    "source",
    ["./local", "/abs", "git::ssh://git@h/a/b.git", "git::file:///r", "git::HTTPS://h/a/b"],
)
def test_any_mcp_config_sources_remote_only(source):
    cfg = {
        "plugins": [{"name": "p0", "source": "github.com/a/b"}, {"name": "p1", "source": source}]
    }
    errs = check(SessionSpec(any_mcp_config=cfg))
    assert len(errs) == 1 and re.match(r"any_mcp_config\.plugins(\[1\]|\.1)\.source", errs[0])
    ok = {"plugins": [{"name": "p0", "source": REMOTE[0]}, {"name": "p1", "plugin": "m:C"}]}
    assert check(SessionSpec(any_mcp_config=ok)) == []


async def test_api_refuses_invalid_source(http):
    r = await http.post(
        "/api/v1/presets",
        json={"name": "p", "spec": {"any_mcp_sources": [REMOTE[0], "./local"]}},
    )
    assert r.status_code == 422, r.text
    msg = r.text
    assert "any_mcp_sources" in msg and "[1]" in msg
