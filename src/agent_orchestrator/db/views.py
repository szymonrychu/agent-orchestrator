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
         WHERE m.cluster_id = c.id AND m.status = 'failed') AS failed
FROM clusters c
LEFT JOIN agents a ON a.cluster_id = c.id
LEFT JOIN agent_status s ON s.agent_id = a.id
{where}
GROUP BY c.id
"""

_AGENTS = """
SELECT a.id, a.ordinal, a.pod_name, a.ready, a.gone_at,
       s.state, s.free_since, s.current_turn_id, s.context_used_tokens,
       s.context_window_tokens, s.context_pct, s.peers
FROM agents a
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
