"""agents table repository. The reconciler is the writer (D3).

`last_dispatched_at` is written only by the dispatch claim (db/queue.py, D23). Every change
that the ribbon shows (pod readiness, gone, session) NOTIFYs `status {cluster_id}`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_orchestrator.db.pool import notify

_COLUMNS = (
    "id, cluster_id, ordinal, pod_name, url, pod_phase, ready, session_id,"
    " session_created_at, gone_at, last_dispatched_at"
)


@dataclass(frozen=True)
class Agent:
    id: uuid.UUID
    cluster_id: uuid.UUID
    ordinal: int
    pod_name: str
    url: str | None
    pod_phase: str | None
    ready: bool
    session_id: str | None
    session_created_at: datetime | None
    gone_at: datetime | None
    last_dispatched_at: datetime | None


async def _one(conn: psycopg.AsyncConnection, query: str, params: Any) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchone()


async def _status(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> None:
    await notify(conn, "status", {"cluster_id": str(cluster_id)})


async def upsert_agent(
    conn: psycopg.AsyncConnection,
    cluster_id: uuid.UUID,
    ordinal: int,
    pod_name: str,
    url: str | None,
    pod_phase: str | None,
    ready: bool,
) -> Agent:
    """Insert or update the agent of one ordinal; an observed pod is never gone.

    The first insert also creates its `agent_status` row with state `unknown`. NOTIFYs
    `status` only when something changed, so a steady state is silent.
    """
    async with conn.transaction():
        before = await _one(
            conn,
            f"SELECT {_COLUMNS} FROM agents WHERE cluster_id = %s AND ordinal = %s FOR UPDATE",
            (cluster_id, ordinal),
        )
        row = await _one(
            conn,
            "INSERT INTO agents (id, cluster_id, ordinal, pod_name, url, pod_phase, ready)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (cluster_id, ordinal) DO UPDATE SET pod_name = EXCLUDED.pod_name,"
            " url = EXCLUDED.url, pod_phase = EXCLUDED.pod_phase, ready = EXCLUDED.ready,"
            f" gone_at = NULL RETURNING {_COLUMNS}",
            (uuid.uuid4(), cluster_id, ordinal, pod_name, url, pod_phase, ready),
        )
        assert row is not None
        await conn.execute(
            "INSERT INTO agent_status (agent_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (row["id"],),
        )
        if before != row:
            await _status(conn, cluster_id)
        return Agent(**row)


async def mark_gone(conn: psycopg.AsyncConnection, agent_id: uuid.UUID) -> bool:
    """Its pod vanished for good (scaled away): never picked again. False when already gone."""
    async with conn.transaction():
        row = await _one(
            conn,
            "UPDATE agents SET gone_at = now(), ready = false"
            " WHERE id = %s AND gone_at IS NULL RETURNING cluster_id",
            (agent_id,),
        )
        if row is None:
            return False
        await _status(conn, row["cluster_id"])
        return True


async def set_session(
    conn: psycopg.AsyncConnection,
    agent_id: uuid.UUID,
    session_id: str,
    created_at: datetime | None,
) -> bool:
    """Record the agent's wrapper session; `created_at` None means the database clock."""
    async with conn.transaction():
        row = await _one(
            conn,
            "UPDATE agents SET session_id = %s, session_created_at = coalesce(%s, now())"
            " WHERE id = %s RETURNING cluster_id",
            (session_id, created_at, agent_id),
        )
        if row is None:
            return False
        await _status(conn, row["cluster_id"])
        return True


async def clear_session(conn: psycopg.AsyncConnection, agent_id: uuid.UUID) -> bool:
    async with conn.transaction():
        row = await _one(
            conn,
            "UPDATE agents SET session_id = NULL, session_created_at = NULL"
            " WHERE id = %s RETURNING cluster_id",
            (agent_id,),
        )
        if row is None:
            return False
        await _status(conn, row["cluster_id"])
        return True


async def get_agent(conn: psycopg.AsyncConnection, agent_id: uuid.UUID) -> Agent | None:
    row = await _one(conn, f"SELECT {_COLUMNS} FROM agents WHERE id = %s", (agent_id,))
    return Agent(**row) if row is not None else None


async def list_agents(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> list[Agent]:
    """Every agent of the cluster, gone ones included, by ordinal."""
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"SELECT {_COLUMNS} FROM agents WHERE cluster_id = %s ORDER BY ordinal",
            (cluster_id,),
        )
        return [Agent(**r) for r in await cur.fetchall()]
