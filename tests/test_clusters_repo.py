import inspect

import pytest
from factories import (
    insert_agent,
    insert_check,
    insert_command,
    insert_event,
    insert_message,
    insert_turn,
)
from pydantic import ValidationError

from agent_orchestrator.db import clusters
from agent_orchestrator.db.clusters import ClusterExists
from agent_orchestrator.settings import Settings
from agent_orchestrator.spec import ClusterSpec, SpecInvalid, validate_slug, validate_spec


def _settings(**kw) -> Settings:
    kw.setdefault("database_url", "postgresql://x/y")
    kw.setdefault("agent_image", "img:1")
    return Settings(**kw)


async def test_create_sets_generation_1(pool):
    async with pool.connection() as conn:
        c = await clusters.create(conn, "alpha", ClusterSpec(replicas=3))
        got = await clusters.get(conn, c.id)
        by_slug = await clusters.get_by_slug(conn, "alpha")
    assert (got.generation, got.observed_generation, got.desired) == (1, 0, "active")
    assert got.spec.replicas == 3 and by_slug.id == c.id
    assert await clusters.get_by_slug(conn, "nope") is None


def test_slug_rules():
    for ok in ("a", "my-cluster", "a1", "x" * 40):
        validate_slug(ok)
    for bad in ("", "A", "-a", "a-", "a_b", "a.b", "x" * 41):
        with pytest.raises(ValueError):
            validate_slug(bad)


async def test_delete_marks_deleting(pool):
    async with pool.connection() as conn:
        c = await clusters.create(conn, "alpha", ClusterSpec())
        d = await clusters.request_delete(conn, c.id)
        assert (d.desired, d.generation) == ("deleting", 2)
        again = await clusters.request_delete(conn, c.id)
        assert (again.desired, again.generation) == ("deleting", 2)
        listed = await clusters.list_clusters(conn)
        assert [x.slug for x in listed] == ["alpha"]
        s = await clusters.set_status(conn, c.id, {"phase": "x"}, 2)
        assert s.observed_generation == 2 and s.status == {"phase": "x"}


async def test_purge_cascades_history(pool):
    from factories import insert_cluster

    async with pool.connection() as conn:
        c = await clusters.create(conn, "alpha", ClusterSpec())
    a = await insert_agent(pool, c.id)
    await insert_message(pool, c.id)
    await insert_event(pool, c.id, a["id"])
    await insert_turn(pool, a["id"])
    await insert_command(pool, a["id"])
    await insert_check(pool, c.id)
    other = await insert_cluster(pool)
    async with pool.connection() as conn:
        await clusters.purge(conn, c.id)
        assert await clusters.get(conn, c.id) is None
        for t in ("agents", "messages", "events", "turns", "commands", "checks"):
            cur = await conn.execute(f"SELECT count(*) FROM {t}")
            assert (await cur.fetchone())[0] == 0, t
        assert await clusters.get(conn, other["id"]) is not None


@pytest.mark.parametrize(
    "key", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "lower", "1X", "A-B"]
)
def test_spec_rejects_secret_env(key):
    with pytest.raises(ValidationError):
        ClusterSpec(env={key: "v"})
    ClusterSpec(env={"MY_FLAG": "v"})


async def test_duplicate_slug(pool):
    async with pool.connection() as conn:
        await clusters.create(conn, "alpha", ClusterSpec())
    async with pool.connection() as conn:
        with pytest.raises(ClusterExists):
            await clusters.create(conn, "alpha", ClusterSpec())


@pytest.mark.parametrize(
    "key",
    [
        "POD_NAME",
        "CCW_X",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "HTTP_PROXY",
        "NO_PROXY",
        "OPENAI_BASE_URL",
    ],
)
def test_spec_rejects_reserved_env_keys(key):
    with pytest.raises(ValidationError):
        ClusterSpec(env={key: "v"})


def test_reserved_slugs_rejected():
    for bad in ("orchestrator", "orchestrator-x", "orchestrator-"):
        with pytest.raises(ValueError):
            validate_slug(bad)
    validate_slug("orchestrators")


def test_validate_spec_image_allowlist():
    s = _settings(agent_images="other:2")
    validate_spec(ClusterSpec(), s)
    validate_spec(ClusterSpec(image="img:1"), s)
    validate_spec(ClusterSpec(image="other:2"), s)
    with pytest.raises(SpecInvalid) as e:
        validate_spec(ClusterSpec(image="evil:3"), s)
    assert any("image" in m for m in e.value.errors)


def test_validate_spec_caps():
    s = _settings(max_replicas=4, max_pvc_size="10Gi")
    validate_spec(ClusterSpec(replicas=4, pvc_size="10Gi"), s)
    validate_spec(ClusterSpec(pvc_size="5120Mi"), s)
    with pytest.raises(SpecInvalid) as e:
        validate_spec(ClusterSpec(replicas=5, pvc_size="11Gi"), s)
    # max_replicas defaults to replicas, so the replicas cap is reported for both fields
    assert len(e.value.errors) == 3
    assert sum("replicas" in m for m in e.value.errors) == 2
    with pytest.raises(SpecInvalid):
        validate_spec(ClusterSpec(pvc_size="1Ti"), s)
    with pytest.raises(ValidationError):
        ClusterSpec(pvc_size="lots")
    with pytest.raises(ValidationError):
        ClusterSpec(replicas=17)


def test_validate_spec_any_mcp_config_schema():
    s = _settings()
    validate_spec(ClusterSpec(any_mcp_config={"python_path": []}), s)
    with pytest.raises(SpecInvalid) as e:
        validate_spec(ClusterSpec(any_mcp_config={"bogus_key": 1}), s)
    assert any("any_mcp_config" in m for m in e.value.errors)


async def test_count_active_counts_rows(pool):
    async with pool.connection() as conn:
        assert await clusters.count_active(conn) == 0
        a = await clusters.create(conn, "a", ClusterSpec())
        await clusters.create(conn, "b", ClusterSpec())
        await clusters.request_delete(conn, a.id)
        assert await clusters.count_active(conn) == 2
        await clusters.purge(conn, a.id)
        assert await clusters.count_active(conn) == 1


def test_spec_retry_default_and_discard_hook_url():
    assert ClusterSpec().max_attempts == 5
    ClusterSpec(discard_hook_url="https://h/x")
    ClusterSpec(discard_hook_url="http://h/x")
    for bad in ("ftp://h/x", "file:///etc/passwd", "h/x", "http://"):
        with pytest.raises(ValidationError):
            ClusterSpec(discard_hook_url=bad)
    for n in (0, 11):
        with pytest.raises(ValidationError):
            ClusterSpec(max_attempts=n)
    with pytest.raises(ValidationError):
        ClusterSpec(unknown=1)
    with pytest.raises(ValidationError):
        ClusterSpec(auto_compact_pct=0)
    with pytest.raises(ValidationError):
        ClusterSpec(workdir="Bad_Dir")
    validate_spec(ClusterSpec(discard_hook_url="https://h/x"), _settings())


@pytest.mark.parametrize("bad", [" 10Gi", "10Gi ", "1Gi\n", "-1Gi", "0"])
def test_spec_rejects_malformed_pvc_size(bad):
    with pytest.raises(ValidationError):
        ClusterSpec(pvc_size=bad)


def test_trailing_newline_rejected():
    with pytest.raises(ValueError):
        validate_slug("abc\n")
    with pytest.raises(ValidationError):
        ClusterSpec(workdir="wd\n")
    with pytest.raises(ValidationError):
        ClusterSpec(env={"FOO\n": "x"})


def test_live_edit_code_retired():
    # names are split so the retired symbols never appear as a literal in the tree
    for name in ("update_" + "spec", "Cluster" + "Deleting"):
        assert not hasattr(clusters, name), name
    assert "stored" in inspect.signature(validate_spec).parameters


def test_validate_spec_stored_exception_kept():
    settings = _settings(models="sonnet,opus", efforts="low,high")
    stored = ClusterSpec(model="legacy-model", effort="legacy-effort")
    # unchanged out-of-list values pass only against the stored spec
    validate_spec(stored, settings, stored)
    with pytest.raises(SpecInvalid):
        validate_spec(stored, settings)
    # a changed out-of-list value is rejected even with a stored spec
    with pytest.raises(SpecInvalid):
        validate_spec(ClusterSpec(model="other-model", effort="legacy-effort"), settings, stored)
    with pytest.raises(SpecInvalid):
        validate_spec(ClusterSpec(model="legacy-model", effort="other-effort"), settings, stored)
