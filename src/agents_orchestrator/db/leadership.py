"""Leadership queue and the leadership pass (D7, D19, D21, D27).

The api only inserts `leadership_requests` rows (status `queued`) and reads the leader record.
`cluster_leaders` and every request decision belong to the dispatcher, through
`leadership_pass`, which runs under the per-cluster claim lock of `db/queue.py` so a leader
change and a message claim never interleave.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agents_orchestrator.db.pool import notify
from agents_orchestrator.db.queue import _CLAIM_LOCK_NAMESPACE

CHANNEL = "leadership_requested"
LEADER_CHANGED_CHANNEL = "leader_changed"
# A leader not ready for longer than this is replaced (D14, a module constant by design).
LEADER_GRACE_SECONDS = 30


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


# ---------------------------------------------------------------- the pass (dispatcher side)


@dataclass(frozen=True)
class LeaderChange:
    cluster_id: uuid.UUID
    agent_id: uuid.UUID
    epoch: int
    reason: str


_HIVE = "coalesce(c.spec->>'max_replicas', c.spec->>'replicas')::int > 1"

# Hive clusters whose leadership needs a pass: queued requests, no leader row while an agent
# could lead, a leader that is gone or not ready, or an unready mark to clear.
_CANDIDATES = f"""
SELECT c.id FROM clusters c
LEFT JOIN cluster_leaders l ON l.cluster_id = c.id
LEFT JOIN agents a ON a.id = l.agent_id
WHERE c.desired = 'active' AND {_HIVE} AND (
    EXISTS (SELECT 1 FROM leadership_requests r
            WHERE r.cluster_id = c.id AND r.status = 'queued')
    OR (l.cluster_id IS NULL AND EXISTS (
        SELECT 1 FROM agents x WHERE x.cluster_id = c.id AND x.ready
          AND x.gone_at IS NULL AND x.session_id IS NOT NULL))
    OR a.gone_at IS NOT NULL OR NOT a.ready OR l.unready_since IS NOT NULL
    OR (c.spec->>'leader_lease_seconds' IS NOT NULL
        AND l.since < now() - make_interval(secs => (c.spec->>'leader_lease_seconds')::int))
)
ORDER BY c.id
"""

# The ready agent free the longest (the _PICK order, free_since asc then id), idle agents
# first, so a hive whose other members are all busy still gets a leader.
_FREE_LONGEST = """
SELECT a.id FROM agents a LEFT JOIN agent_status s ON s.agent_id = a.id
WHERE a.cluster_id = %s AND a.ready AND a.gone_at IS NULL AND a.session_id IS NOT NULL
  AND a.id <> %s
ORDER BY (s.state = 'idle') IS NOT TRUE, s.free_since ASC NULLS LAST, a.id
LIMIT 1
"""


async def _one(conn: psycopg.AsyncConnection, query: str, params: Any = ()) -> dict | None:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchone()


async def _all(conn: psycopg.AsyncConnection, query: str, params: Any = ()) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def leadership_candidates(conn: psycopg.AsyncConnection) -> list[uuid.UUID]:
    """Hive clusters the dispatcher runs `leadership_pass` for on this wake (one query)."""
    return [r["id"] for r in await _all(conn, _CANDIDATES)]


async def hive_clusters(conn: psycopg.AsyncConnection) -> list[dict]:
    """Active hive clusters that have a leader row: id, slug, spec (the push loop's set)."""
    return await _all(
        conn,
        "SELECT c.id, c.slug, c.spec FROM clusters c"
        " JOIN cluster_leaders l ON l.cluster_id = c.id"
        f" WHERE c.desired = 'active' AND {_HIVE} ORDER BY c.id",
    )


async def _leader(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> dict | None:
    return await _one(
        conn,
        "SELECT l.agent_id, l.epoch, l.unready_since,"
        " coalesce(l.since < now() - make_interval(secs => (c.spec->>'leader_lease_seconds')::int),"
        "   false) AS lease_over,"
        " EXISTS (SELECT 1 FROM agent_status s WHERE s.agent_id = l.agent_id"
        "   AND s.state = 'idle')"
        " AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.agent_id = l.agent_id"
        "   AND m.status IN ('dispatching', 'verifying')) AS at_boundary,"
        " l.unready_since IS NOT NULL AND l.unready_since"
        "   < now() - make_interval(secs => %s) AS grace_over,"
        " a.ready, a.gone_at IS NOT NULL AS gone"
        " FROM cluster_leaders l JOIN agents a ON a.id = l.agent_id"
        " JOIN clusters c ON c.id = l.cluster_id"
        " WHERE l.cluster_id = %s FOR UPDATE OF l",
        (LEADER_GRACE_SECONDS, cluster_id),
    )


async def _free_longest(
    conn: psycopg.AsyncConnection, cluster_id: uuid.UUID, other_than: uuid.UUID
) -> uuid.UUID | None:
    row = await _one(conn, _FREE_LONGEST, (cluster_id, other_than))
    return row["id"] if row else None


async def _target_ready(
    conn: psycopg.AsyncConnection, cluster_id: uuid.UUID, agent_id: uuid.UUID | None
) -> bool:
    if agent_id is None:
        return False
    row = await _one(
        conn,
        "SELECT 1 AS ok FROM agents WHERE id = %s AND cluster_id = %s AND ready"
        " AND gone_at IS NULL",
        (agent_id, cluster_id),
    )
    return row is not None


async def _set_leader(
    conn: psycopg.AsyncConnection,
    cluster_id: uuid.UUID,
    agent_id: uuid.UUID,
    reason: str,
) -> LeaderChange:
    """Insert the first leader row (epoch 1) or move the existing one to `epoch + 1`.

    A new row starts its feed cursor at the cluster's newest event, so history is never
    pushed as current activity. Every change NOTIFYs `leader_changed`.
    """
    row = await _one(
        conn,
        "INSERT INTO cluster_leaders (cluster_id, agent_id, epoch, since, reason, feed_cursor)"
        " VALUES (%(c)s, %(a)s, 1, now(), %(r)s,"
        "  (SELECT coalesce(max(id), 0) FROM events WHERE cluster_id = %(c)s))"
        " ON CONFLICT (cluster_id) DO UPDATE SET agent_id = EXCLUDED.agent_id,"
        "  epoch = cluster_leaders.epoch + 1, since = now(), reason = EXCLUDED.reason,"
        "  unready_since = NULL"
        " RETURNING epoch",
        {"c": cluster_id, "a": agent_id, "r": reason},
    )
    assert row is not None
    change = LeaderChange(cluster_id, agent_id, row["epoch"], reason)
    await notify(
        conn,
        LEADER_CHANGED_CHANNEL,
        {"cluster_id": str(cluster_id), "agent_id": str(agent_id), "epoch": change.epoch},
    )
    return change


async def _decide(
    conn: psycopg.AsyncConnection, request_id: int, reject_reason: str | None
) -> bool:
    """Decide one queued request (status-guarded); False when it was no longer queued."""
    cur = await conn.execute(
        "UPDATE leadership_requests SET status = %s, reject_reason = %s, decided_at = now()"
        " WHERE id = %s AND status = 'queued'",
        ("rejected" if reject_reason else "applied", reject_reason, request_id),
    )
    return cur.rowcount == 1


async def _judge(
    conn: psycopg.AsyncConnection, cluster_id: uuid.UUID, req: dict, leader: dict | None
) -> tuple[str | None, uuid.UUID | None, str]:
    """(reject reason or None, new leader or None for no change, change reason)."""
    current = leader["agent_id"] if leader else None
    if req["kind"] == "user":
        # Q10 override: no epoch fence, a target that already leads is applied unchanged.
        if not await _target_ready(conn, cluster_id, req["to_agent_id"]):
            return "target_not_ready", None, ""
        target = req["to_agent_id"]
        return None, (None if target == current else target), "user"
    if leader is None or req["from_agent_id"] != current:
        return "not_leader", None, ""
    if req["epoch"] != leader["epoch"]:
        return "stale_epoch", None, ""
    reason = f"handover: {req['reason']}"
    if req["to_agent_id"] is not None:
        target = req["to_agent_id"]
        if target == current or not await _target_ready(conn, cluster_id, target):
            return "target_not_ready", None, ""
        return None, target, reason
    picked = await _free_longest(conn, cluster_id, current)
    if picked is None:
        return "no_peers", None, ""
    return None, picked, reason


async def leadership_pass(
    conn: psycopg.AsyncConnection, cluster_id: uuid.UUID
) -> LeaderChange | None:
    """Elect, apply queued requests, replace a lost leader: one transaction, one lock.

    Returns the last change of this pass, or None when nothing changed, the cluster is not a
    hive, or another dispatcher holds the cluster's claim lock (the pass is skipped).
    """
    async with conn.transaction():
        lock = await _one(
            conn,
            "SELECT pg_try_advisory_xact_lock(%s, hashtext(%s::text)) AS got",
            (_CLAIM_LOCK_NAMESPACE, cluster_id),
        )
        if not (lock and lock["got"]):
            return None
        hive = await _one(
            conn, f"SELECT {_HIVE} AS hive FROM clusters c WHERE c.id = %s", (cluster_id,)
        )
        if not (hive and hive["hive"]):
            return None
        change: LeaderChange | None = None

        # 1. Initial leader (Q2): the lowest-ordinal ready agent with a session.
        leader = await _leader(conn, cluster_id)
        if leader is None:
            first = await _one(
                conn,
                "SELECT id FROM agents WHERE cluster_id = %s AND ready AND gone_at IS NULL"
                " AND session_id IS NOT NULL ORDER BY ordinal LIMIT 1",
                (cluster_id,),
            )
            if first is not None:
                change = await _set_leader(conn, cluster_id, first["id"], "initial")
                leader = await _leader(conn, cluster_id)

        # 2. Queued requests in id order, each re-checked against the leader as it is now.
        for req in await _all(
            conn,
            "SELECT id, kind, from_agent_id, to_agent_id, reason, epoch"
            " FROM leadership_requests WHERE cluster_id = %s AND status = 'queued'"
            " ORDER BY id FOR UPDATE",
            (cluster_id,),
        ):
            reject, target, reason = await _judge(conn, cluster_id, req, leader)
            if not await _decide(conn, req["id"], reject):
                continue
            if target is not None:
                change = await _set_leader(conn, cluster_id, target, reason)
                leader = await _leader(conn, cluster_id)

        # 3. Lost leader: gone, or not ready past the grace (unready_since set on first sight
        # and cleared once ready again, so separate short windows never add up).
        if leader is not None:
            if leader["ready"] and not leader["gone"]:
                if leader["unready_since"] is not None:
                    await conn.execute(
                        "UPDATE cluster_leaders SET unready_since = NULL WHERE cluster_id = %s",
                        (cluster_id,),
                    )
            elif leader["gone"] or leader["grace_over"]:
                picked = await _free_longest(conn, cluster_id, leader["agent_id"])
                if picked is not None:
                    change = await _set_leader(conn, cluster_id, picked, "leader_lost")
                    leader = None
            elif leader["unready_since"] is None:
                await conn.execute(
                    "UPDATE cluster_leaders SET unready_since = now() WHERE cluster_id = %s",
                    (cluster_id,),
                )

        # 4. Lease expiry (hive D26 amended): a healthy leader whose term outlived the spec's
        # `leader_lease_seconds` hands over at a turn boundary, never mid-turn and never as a
        # takeover. With no ready peer the leader stays and the lease is re-checked next pass.
        if (
            leader is not None
            and leader["lease_over"]
            and leader["ready"]
            and not leader["gone"]
            and leader["at_boundary"]
        ):
            picked = await _free_longest(conn, cluster_id, leader["agent_id"])
            if picked is not None:
                change = await _set_leader(conn, cluster_id, picked, "lease_expired")
        return change
