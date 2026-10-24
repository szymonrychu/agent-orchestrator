import asyncio
import threading

import psycopg
import pytest
from factories import (
    insert_agent,
    insert_check,
    insert_cluster,
    insert_command,
    insert_event,
    insert_message,
    insert_turn,
    set_agent_status,
)

from agent_orchestrator.db.migrate import run_migrations
from agent_orchestrator.db.pool import listen, notify

TABLES = {
    "clusters",
    "agents",
    "agent_status",
    "messages",
    "turns",
    "events",
    "commands",
    "checks",
    "presets",
    "schema_migrations",
}


def _columns(dsn: str, table: str) -> set[str]:
    with psycopg.connect(dsn) as c:
        rows = c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s", (table,)
        ).fetchall()
    return {r[0] for r in rows}


def test_fresh_database_gets_all_tables(empty_dsn):
    run_migrations(empty_dsn)
    with psycopg.connect(empty_dsn) as c:
        tables = {
            r[0]
            for r in c.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"
            )
        }
        versions = [r[0] for r in c.execute("SELECT version FROM schema_migrations")]
    assert tables >= TABLES
    assert versions == ["0001", "0002"]


def test_rerun_is_noop(empty_dsn):
    run_migrations(empty_dsn)
    with psycopg.connect(empty_dsn) as c:
        before = c.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    assert run_migrations(empty_dsn) == []
    with psycopg.connect(empty_dsn) as c:
        after = c.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    assert before == after


def test_concurrent_migrate_serialises(empty_dsn):
    results: list = []
    errors: list = []

    def go():
        try:
            results.append(run_migrations(empty_dsn))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert sorted(len(r) for r in results) == [0, 2]
    with psycopg.connect(empty_dsn) as c:
        assert c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2


async def test_events_unique_agent_session_seq(pool):
    cl = await insert_cluster(pool)
    ag = await insert_agent(pool, cl["id"])
    await insert_event(pool, cl["id"], ag["id"], session_id="s", seq=1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        await insert_event(pool, cl["id"], ag["id"], session_id="s", seq=1)
    await insert_event(pool, cl["id"], ag["id"], session_id="s2", seq=1)


async def test_cluster_delete_cascades(pool):
    cl = await insert_cluster(pool)
    ag = await insert_agent(pool, cl["id"])
    await set_agent_status(pool, ag["id"])
    await insert_message(pool, cl["id"])
    await insert_event(pool, cl["id"], ag["id"])
    await insert_turn(pool, ag["id"])
    await insert_command(pool, ag["id"])
    await insert_check(pool, cl["id"])
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM clusters WHERE id=%s", (cl["id"],))
        for t in ("agents", "agent_status", "messages", "events", "turns", "commands", "checks"):
            cur = await conn.execute(f"SELECT count(*) FROM {t}")
            assert (await cur.fetchone())[0] == 0, t


async def test_notify_listen_roundtrip(dsn, pool):
    stream = listen(dsn, ["orch_test"])
    got: list = []

    async def reader():
        async for item in stream:
            got.append(item)
            return

    task = asyncio.create_task(reader())
    for _ in range(50):
        await asyncio.sleep(0.1)
        async with pool.connection() as conn:
            await notify(conn, "orch_test", {"a": 1, "b": [2]})
        if got:
            break
    await asyncio.wait_for(task, 5)
    assert got[0] == ("orch_test", {"a": 1, "b": [2]})
    await stream.aclose()


async def test_listen_reconnects_after_connection_killed(dsn, pool):
    stream = listen(dsn, ["orch_rc"], initial_backoff=0.05, max_backoff=0.2)
    got: list = []

    async def reader():
        async for item in stream:
            got.append(item)
            if len(got) == 2:
                return

    async def wait_listener() -> None:
        for _ in range(100):
            async with pool.connection() as conn:
                cur = await conn.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE query LIKE 'LISTEN%%'"
                    " AND pid <> pg_backend_pid() AND datname = current_database()"
                )
                if (await cur.fetchone())[0]:
                    return
            await asyncio.sleep(0.05)
        raise AssertionError("listener never connected")

    task = asyncio.create_task(reader())
    await wait_listener()
    async with pool.connection() as conn:
        await notify(conn, "orch_rc", {"n": 1})
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.05)
    assert got == [("orch_rc", {"n": 1})]
    async with pool.connection() as conn:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
            " WHERE query LIKE 'LISTEN%%' AND pid <> pg_backend_pid()"
            " AND datname = current_database()"
        )
    for _ in range(100):
        await asyncio.sleep(0.1)
        async with pool.connection() as conn:
            await notify(conn, "orch_rc", {"n": 2})
        if len(got) == 2:
            break
    await asyncio.wait_for(task, 5)
    assert got[1] == ("orch_rc", {"n": 2})
    await stream.aclose()


async def test_dispatch_columns_and_rebootstrap_kind(pool):
    assert "last_dispatched_at" in _columns(pool.conninfo, "agents")
    assert "wrapper_idle_since" in _columns(pool.conninfo, "agent_status")
    assert "heartbeat_at" in _columns(pool.conninfo, "messages")
    cl = await insert_cluster(pool)
    ag = await insert_agent(pool, cl["id"])
    cmd = await insert_command(pool, ag["id"], kind="rebootstrap")
    assert cmd["kind"] == "rebootstrap"
    with pytest.raises(psycopg.errors.CheckViolation):
        await insert_command(pool, ag["id"], kind="bogus")


async def test_retry_columns_verifying_status_and_no_purge_columns(pool):
    cols = _columns(pool.conninfo, "messages")
    assert {"not_before", "ambiguous_at"} <= cols
    cl = await insert_cluster(pool)
    m = await insert_message(pool, cl["id"], status="verifying")
    assert m["status"] == "verifying"
    with pytest.raises(psycopg.errors.CheckViolation):
        await insert_message(pool, cl["id"], status="nope")
    ccols = _columns(pool.conninfo, "clusters")
    assert "deleted_at" not in ccols
    assert not any("purge" in c for c in ccols)


DEFAULT_PRESET_ID = "00000000-0000-0000-0000-000000000001"


def test_default_preset_seeded(dsn):
    with psycopg.connect(dsn) as c:
        rows = c.execute("SELECT id::text, name, spec, is_default FROM presets").fetchall()
        assert rows == [(DEFAULT_PRESET_ID, "Default", {"replicas": 1}, True)]
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("INSERT INTO presets (id, name) VALUES (gen_random_uuid(), 'default')")
    with psycopg.connect(dsn) as c, pytest.raises(psycopg.errors.UniqueViolation):
        c.execute(
            "INSERT INTO presets (id, name, is_default) VALUES (gen_random_uuid(), 'x', true)"
        )
    with psycopg.connect(dsn) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("INSERT INTO presets (id, name) VALUES (gen_random_uuid(), '')")
    with psycopg.connect(dsn) as c, pytest.raises(psycopg.errors.CheckViolation):
        c.execute("INSERT INTO presets (id, name) VALUES (gen_random_uuid(), %s)", ("x" * 41,))


def test_presets_migration_reapply_keeps_edited_default(empty_dsn):
    run_migrations(empty_dsn)
    with psycopg.connect(empty_dsn, autocommit=True) as c:
        c.execute("UPDATE presets SET spec = '{\"replicas\": 4}' WHERE is_default")
        c.execute("DELETE FROM schema_migrations WHERE version = '0002'")
    assert run_migrations(empty_dsn) == ["0002"]
    with psycopg.connect(empty_dsn) as c:
        assert c.execute("SELECT name, spec FROM presets").fetchall() == [
            ("Default", {"replicas": 4})
        ]
        assert c.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 2


async def test_preset_delete_keeps_cluster_provenance(pool):
    cols = _columns(pool.conninfo, "clusters")
    assert {"preset_id", "preset_name"} <= cols
    cl = await insert_cluster(pool)
    async with pool.connection() as conn:
        assert await (
            await conn.execute(
                "SELECT preset_id, preset_name FROM clusters WHERE id=%s", (cl["id"],)
            )
        ).fetchone() == (None, None)
        await conn.execute(
            "INSERT INTO presets (id, name) VALUES (%s, 'Big')",
            ("00000000-0000-0000-0000-0000000000aa",),
        )
        await conn.execute(
            "UPDATE clusters SET preset_id=%s, preset_name='Big' WHERE id=%s",
            ("00000000-0000-0000-0000-0000000000aa", cl["id"]),
        )
        await conn.execute("DELETE FROM presets WHERE name='Big'")
        cur = await conn.execute(
            "SELECT preset_id, preset_name FROM clusters WHERE id=%s", (cl["id"],)
        )
        assert await cur.fetchone() == (None, "Big")
