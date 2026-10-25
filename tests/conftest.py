"""Shared fixtures: one Postgres container, migrated once into a template database."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from testcontainers.postgres import PostgresContainer

from agents_orchestrator.db.migrate import run_migrations
from agents_orchestrator.db.pool import open_pool

TEMPLATE_DB = "orch_template"


def _plain_dsn(container: PostgresContainer, dbname: str) -> str:
    return (
        f"postgresql://{container.username}:{container.password}"
        f"@{container.get_container_host_ip()}:{container.get_exposed_port(5432)}/{dbname}"
    )


@pytest.fixture(scope="session")
def pg_container() -> Iterator[PostgresContainer]:
    with PostgresContainer("postgres:18-alpine", driver=None) as container:
        admin = _plain_dsn(container, container.dbname)
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEMPLATE_DB)))
        run_migrations(_plain_dsn(container, TEMPLATE_DB))
        yield container


@pytest.fixture(scope="session")
def admin_dsn(pg_container: PostgresContainer) -> str:
    return _plain_dsn(pg_container, pg_container.dbname)


@pytest.fixture
def dsn(pg_container: PostgresContainer, admin_dsn: str) -> Iterator[str]:
    """A fresh database cloned from the migrated template."""
    name = f"t_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(TEMPLATE_DB)
            )
        )
    yield _plain_dsn(pg_container, name)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest.fixture
def empty_dsn(pg_container: PostgresContainer, admin_dsn: str) -> Iterator[str]:
    """A fresh, unmigrated database."""
    name = f"e_{uuid.uuid4().hex}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    yield _plain_dsn(pg_container, name)
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


@pytest_asyncio.fixture
async def pool(dsn: str) -> AsyncIterator:
    p = await open_pool(dsn)
    try:
        yield p
    finally:
        await p.close()
