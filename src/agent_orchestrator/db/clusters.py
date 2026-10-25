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


class ClusterBeingDeleted(Exception):
    """The cluster is being deleted and takes no more edits."""


class ScaleRefused(Exception):
    """A replica change was refused; `code` is one of cluster_deleting, not_leader,
    static_cluster, out_of_range, leader_would_leave."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class ScaleOutcome:
    cluster_id: uuid.UUID
    replicas: int
    previous_replicas: int
    max_replicas: int


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


async def close_expired(conn: psycopg.AsyncConnection) -> list[uuid.UUID]:
    """Flip active clusters past their autoclose time to deleting (the request_delete write)."""
    async with conn.transaction():
        cur = await conn.execute(
            "UPDATE clusters SET desired = 'deleting', generation = generation + 1"
            " WHERE desired = 'active' AND spec ? 'autoclose_after_seconds'"
            " AND created_at + make_interval(secs => (spec->>'autoclose_after_seconds')::int)"
            " <= now() RETURNING id"
        )
        ids = [row[0] for row in await cur.fetchall()]
        for cluster_id in ids:
            await _notify(conn, cluster_id)
    return ids


async def set_init_scripts(
    conn: psycopg.AsyncConnection, cluster_id: uuid.UUID, scripts: list[str]
) -> tuple[Cluster, bool]:
    """Write the init scripts to the cluster spec and to its source preset, in one transaction.

    No generation bump: the list is never rendered into pods. Lock order is clusters, then
    presets. Returns the updated cluster and whether a preset row was written.
    """
    async with conn.transaction():
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT desired, preset_id FROM clusters WHERE id = %s FOR UPDATE", (cluster_id,)
            )
            locked = await cur.fetchone()
        if locked is None:
            raise ClusterNotFound(str(cluster_id))
        desired, preset_id = locked
        if desired == "deleting":
            raise ClusterBeingDeleted(str(cluster_id))
        listed = Jsonb(scripts)
        rows = await _fetch(
            conn,
            "UPDATE clusters SET spec = jsonb_set(spec, '{init_scripts}', %s) WHERE id = %s"
            " RETURNING *",
            (listed, cluster_id),
        )
        preset_written = False
        if preset_id is not None:
            cur = await conn.execute("SELECT 1 FROM presets WHERE id = %s FOR UPDATE", (preset_id,))
            if await cur.fetchone() is not None:
                await conn.execute(
                    "UPDATE presets SET spec = jsonb_set(spec, '{init_scripts}', %s),"
                    " updated_at = now() WHERE id = %s",
                    (listed, preset_id),
                )
                preset_written = True
        await _notify(conn, cluster_id)
    return rows[0], preset_written


async def set_replicas(
    conn: psycopg.AsyncConnection,
    cluster_id: uuid.UUID,
    replicas: int,
    *,
    caller_agent_id: uuid.UUID | None = None,
) -> ScaleOutcome:
    """The one writer of the runtime replica count: spec.replicas, in place.

    No generation bump: a replica change never touches the pod template. Lock order is the
    cluster row, then a read of cluster_leaders joined to agents (never written here).
    Refusals, in order: cluster_deleting, not_leader, static_cluster, out_of_range,
    leader_would_leave. An unchanged value writes and notifies nothing.
    """
    async with conn.transaction():
        locked = await _fetch(
            conn, "SELECT * FROM clusters WHERE id = %s FOR UPDATE", (cluster_id,)
        )
        if not locked:
            raise ClusterNotFound(str(cluster_id))
        cluster = locked[0]
        spec = cluster.spec
        if cluster.desired != "active":
            raise ScaleRefused("cluster_deleting")
        cur = await conn.execute(
            "SELECT l.agent_id, a.ordinal FROM cluster_leaders l"
            " JOIN agents a ON a.id = l.agent_id WHERE l.cluster_id = %s",
            (cluster_id,),
        )
        leader = await cur.fetchone()
        if caller_agent_id is not None and (leader is None or leader[0] != caller_agent_id):
            raise ScaleRefused("not_leader")
        if spec.static:
            raise ScaleRefused("static_cluster")
        max_replicas = spec.max_replicas or spec.replicas
        if not 1 <= replicas <= max_replicas:
            raise ScaleRefused("out_of_range", f"replicas must be 1..{max_replicas}")
        if leader is not None and leader[1] >= replicas:
            raise ScaleRefused("leader_would_leave")
        outcome = ScaleOutcome(cluster_id, replicas, spec.replicas, max_replicas)
        if replicas == spec.replicas:
            return outcome
        await conn.execute(
            "UPDATE clusters SET spec = jsonb_set(spec, '{replicas}', to_jsonb(%s::int))"
            " WHERE id = %s AND desired = 'active'",
            (replicas, cluster_id),
        )
        await _notify(conn, cluster_id)
    return outcome


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
