"""Read-only queries behind the api views."""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
from psycopg.rows import dict_row

from agent_orchestrator.db.clusters import Cluster

_AGGREGATES = """
SELECT c.id AS cluster_id,
       count(a.id) FILTER (WHERE a.ready AND a.gone_at IS NULL) AS ready,
       count(s.agent_id) FILTER (WHERE a.gone_at IS NULL AND s.state = 'busy') AS busy,
       max(s.context_pct) FILTER (WHERE a.gone_at IS NULL) AS context_pct_max,
       (SELECT count(*) FROM messages m
         WHERE m.cluster_id = c.id AND m.status = 'queued') AS queued,
       (SELECT count(*) FROM messages m
         WHERE m.cluster_id = c.id AND m.status = 'failed') AS failed,
       l.agent_id AS leader_agent_id, l.epoch AS leader_epoch
FROM clusters c
LEFT JOIN cluster_leaders l ON l.cluster_id = c.id
LEFT JOIN agents a ON a.cluster_id = c.id
LEFT JOIN agent_status s ON s.agent_id = a.id
{where}
GROUP BY c.id, l.agent_id, l.epoch
"""

_AGENTS = """
SELECT a.id, a.ordinal, a.pod_name, a.ready, a.gone_at,
       s.state, s.free_since, s.current_turn_id, s.context_used_tokens,
       s.context_window_tokens, s.context_pct, s.peers,
       (l.agent_id IS NOT NULL) AS leader
FROM agents a
LEFT JOIN cluster_leaders l ON l.agent_id = a.id
LEFT JOIN agent_status s ON s.agent_id = a.id
WHERE a.cluster_id = %s
ORDER BY a.ordinal
"""


async def cluster_aggregates(
    conn: psycopg.AsyncConnection, cluster_id: uuid.UUID | None = None
) -> dict[uuid.UUID, dict[str, Any]]:
    where, params = ("WHERE c.id = %s", (cluster_id,)) if cluster_id else ("", ())
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_AGGREGATES.format(where=where), params)
        return {r["cluster_id"]: r for r in await cur.fetchall()}


def summarize(
    cluster: Cluster, agg: dict[str, Any] | None, default_auto_compact_pct: int
) -> dict[str, Any]:
    agg = agg or {}
    ready = agg.get("ready", 0)
    if cluster.desired == "deleting":
        state = "deleting"
    elif cluster.status.get("error"):
        state = "error"
    elif agg.get("busy", 0) > 0:
        state = "working"
    elif ready == 0:
        state = "booting"
    else:
        state = "waiting"
    return {
        "state": state,
        "ready": ready,
        "replicas": cluster.spec.replicas,
        "context_pct_max": agg.get("context_pct_max"),
        "auto_compact_pct": cluster.spec.auto_compact_pct or default_auto_compact_pct,
        "queued": agg.get("queued", 0),
        "failed": agg.get("failed", 0),
    }


async def agent_rows(conn: psycopg.AsyncConnection, cluster_id: uuid.UUID) -> list[dict[str, Any]]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(_AGENTS, (cluster_id,))
        return await cur.fetchall()


# ---------------------------------------------------------------- messages, commands, checks

MESSAGE_STATUSES = ("queued", "dispatching", "verifying", "sent", "failed", "cancelled")

_MESSAGE_SELECT = """
SELECT m.id, m.cluster_id, m.target_agent_id, m.agent_id, m.text, m.status, m.turn_id,
       m.attempts, m.not_before, m.error, m.created_at, m.dispatched_at,
       coalesce((c.spec->>'max_attempts')::int, %(default_attempts)s) AS max_attempts,
       t.status AS turn_status, t.started_at AS turn_started_at,
       t.completed_at AS turn_completed_at, t.final_text AS turn_final_text,
       t.usage AS turn_usage,
       (t.turn_id IS NOT NULL) AS has_turn
FROM messages m
JOIN clusters c ON c.id = m.cluster_id
LEFT JOIN turns t ON t.turn_id = m.turn_id
"""


async def _rows(conn: psycopg.AsyncConnection, query: str, params: Any) -> list[dict]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(query, params)
        return await cur.fetchall()


async def get_message(
    conn: psycopg.AsyncConnection, id: int, default_attempts: int
) -> dict[str, Any] | None:
    rows = await _rows(
        conn,
        _MESSAGE_SELECT + " WHERE m.id = %(id)s",
        {"id": id, "default_attempts": default_attempts},
    )
    return rows[0] if rows else None


async def list_messages(
    conn: psycopg.AsyncConnection,
    cluster_id: uuid.UUID,
    statuses: list[str] | None,
    limit: int,
    default_attempts: int,
) -> list[dict[str, Any]]:
    return await _rows(
        conn,
        _MESSAGE_SELECT
        + " WHERE m.cluster_id = %(cid)s AND (%(st)s::text[] IS NULL OR m.status = ANY(%(st)s))"
        " ORDER BY m.id DESC LIMIT %(limit)s",
        {"cid": cluster_id, "st": statuses, "limit": limit, "default_attempts": default_attempts},
    )


async def get_agent_ref(conn: psycopg.AsyncConnection, agent_id: uuid.UUID) -> dict | None:
    rows = await _rows(
        conn,
        "SELECT id, cluster_id, gone_at FROM agents WHERE id = %s",
        (agent_id,),
    )
    return rows[0] if rows else None


async def get_command(conn: psycopg.AsyncConnection, id: int) -> dict | None:
    rows = await _rows(
        conn,
        "SELECT id, agent_id, kind, status, result, created_at, started_at, finished_at"
        " FROM commands WHERE id = %s",
        (id,),
    )
    return rows[0] if rows else None


async def get_check(conn: psycopg.AsyncConnection, id: int) -> dict | None:
    rows = await _rows(
        conn,
        "SELECT id, cluster_id, command, status, exit_code, log_tail, created_at, started_at,"
        " finished_at FROM checks WHERE id = %s",
        (id,),
    )
    return rows[0] if rows else None
