"""Leadership queue, api side: requests are inserted here and decided by the dispatcher.

The api only inserts `leadership_requests` rows (status `queued`) and reads the leader record;
`cluster_leaders` and every status change belong to the dispatcher.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import psycopg

from agent_orchestrator.db.pool import notify

CHANNEL = "leadership_requested"


@dataclass(frozen=True)
class Leader:
    agent_id: uuid.UUID
    pod_name: str
    epoch: int


async def insert_request(
    conn: psycopg.AsyncConnection,
    cluster_id: uuid.UUID,
    kind: str,
    from_agent_id: uuid.UUID | None,
    to_agent_id: uuid.UUID | None,
    reason: str,
    epoch: int | None,
) -> int:
    """Queue a request and NOTIFY `leadership_requested {cluster_id}`; returns its id."""
    async with conn.transaction():
        cur = await conn.execute(
            "INSERT INTO leadership_requests"
            " (cluster_id, kind, from_agent_id, to_agent_id, reason, epoch)"
            " VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (cluster_id, kind, from_agent_id, to_agent_id, reason, epoch),
        )
        row = await cur.fetchone()
        assert row is not None
        await notify(conn, CHANNEL, {"cluster_id": str(cluster_id)})
        return row[0]


async def current_leader(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> Leader | None:
    cur = await conn.execute(
        "SELECT l.agent_id, a.pod_name, l.epoch FROM cluster_leaders l"
        " JOIN agents a ON a.id = l.agent_id WHERE l.cluster_id = %s",
        (cluster_id,),
    )
    row = await cur.fetchone()
    return Leader(agent_id=row[0], pod_name=row[1], epoch=row[2]) if row else None
