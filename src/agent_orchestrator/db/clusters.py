"""clusters table repository. The api writes; the reconciler uses set_status and purge."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from agent_orchestrator.db.pool import notify
from agent_orchestrator.spec import ClusterSpec, validate_slug

CHANNEL = "cluster_changed"


class ClusterExists(Exception):
    """A cluster with this slug already has a row."""


class ClusterNotFound(Exception):
    pass


@dataclass(frozen=True)
class Cluster:
    id: uuid.UUID
    slug: str
    spec: ClusterSpec
    generation: int
    observed_generation: int
    desired: str
    status: dict[str, Any]
    created_at: datetime
    preset_id: uuid.UUID | None = None
    preset_name: str | None = None


def _from_row(row: dict[str, Any]) -> Cluster:
    return Cluster(
        id=row["id"],
        slug=row["slug"],
        spec=ClusterSpec.model_validate(row["spec"]),
        generation=row["generation"],
        observed_generation=row["observed_generation"],
        desired=row["desired"],
        status=row["status"],
        created_at=row["created_at"],
        preset_id=row["preset_id"],
        preset_name=row["preset_name"],
    )


def _dump(spec: ClusterSpec) -> Jsonb:
    return Jsonb(spec.model_dump(mode="json", exclude_none=True))


async def _fetch(conn: psycopg.AsyncConnection, query: str, params: tuple) -> list[Cluster]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return [_from_row(r) for r in await cur.fetchall()]


async def _notify(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> None:
    await notify(conn, CHANNEL, {"cluster_id": str(cluster_id)})


async def create(
    conn: psycopg.AsyncConnection,
    slug: str,
    spec: ClusterSpec,
    preset_id: uuid.UUID | None = None,
    preset_name: str | None = None,
) -> Cluster:
    validate_slug(slug)
    try:
        async with conn.transaction():
            rows = await _fetch(
                conn,
                "INSERT INTO clusters (id, slug, spec, preset_id, preset_name)"
                " VALUES (%s, %s, %s, %s, %s) RETURNING *",
                (uuid.uuid4(), slug, _dump(spec), preset_id, preset_name),
            )
            await _notify(conn, rows[0].id)
    except psycopg.errors.UniqueViolation:
        raise ClusterExists(slug) from None
    return rows[0]


async def get(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> Cluster | None:
    rows = await _fetch(conn, "SELECT * FROM clusters WHERE id = %s", (cluster_id,))
    return rows[0] if rows else None


async def get_by_slug(conn: psycopg.AsyncConnection, slug: str) -> Cluster | None:
    rows = await _fetch(conn, "SELECT * FROM clusters WHERE slug = %s", (slug,))
    return rows[0] if rows else None


async def list_clusters(conn: psycopg.AsyncConnection) -> list[Cluster]:
    return await _fetch(conn, "SELECT * FROM clusters ORDER BY created_at, slug", ())


async def count_active(conn: psycopg.AsyncConnection) -> int:
    cur = await conn.execute("SELECT count(*) FROM clusters")
    return (await cur.fetchone())[0]


async def request_delete(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> Cluster:
    async with conn.transaction():
        rows = await _fetch(
            conn,
            "UPDATE clusters SET desired = 'deleting', generation = generation + 1"
            " WHERE id = %s AND desired = 'active' RETURNING *",
            (cluster_id,),
        )
        if rows:
            await _notify(conn, cluster_id)
            return rows[0]
    existing = await get(conn, cluster_id)
    if existing is None:
        raise ClusterNotFound(str(cluster_id))
    return existing


async def set_status(
    conn: psycopg.AsyncConnection,
    cluster_id: uuid.UUID,
    status: dict[str, Any],
    observed_generation: int,
) -> Cluster:
    async with conn.transaction():
        rows = await _fetch(
            conn,
            "UPDATE clusters SET status = %s, observed_generation = %s WHERE id = %s RETURNING *",
            (Jsonb(status), observed_generation, cluster_id),
        )
        if not rows:
            raise ClusterNotFound(str(cluster_id))
        await _notify(conn, cluster_id)
    return rows[0]


async def purge(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> None:
    """Delete the row; foreign keys cascade agents, messages, events, turns, commands, checks."""
    async with conn.transaction():
        await conn.execute("DELETE FROM clusters WHERE id = %s", (cluster_id,))
        await _notify(conn, cluster_id)
