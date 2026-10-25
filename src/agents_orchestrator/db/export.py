"""The JSONL history export a pre-discard hook receives (D15).

One `cluster` record, then the `agent`, `message`, `turn`, `command`, `check` and `event`
records of that cluster, each kind in id order (turns in started_at order). Every kind is read
through a server-side cursor, so a long history is never held in memory.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

FETCH_SIZE = 500

# kind -> query; every one takes the cluster id as %(c)s.
QUERIES: tuple[tuple[str, str], ...] = (
    ("agent", "SELECT * FROM agents WHERE cluster_id = %(c)s ORDER BY id"),
    ("message", "SELECT * FROM messages WHERE cluster_id = %(c)s ORDER BY id"),
    (
        "turn",
        "SELECT t.* FROM turns t JOIN agents a ON a.id = t.agent_id"
        " WHERE a.cluster_id = %(c)s ORDER BY t.started_at, t.turn_id",
    ),
    (
        "command",
        "SELECT c.* FROM commands c JOIN agents a ON a.id = c.agent_id"
        " WHERE a.cluster_id = %(c)s ORDER BY c.id",
    ),
    ("check", "SELECT * FROM checks WHERE cluster_id = %(c)s ORDER BY id"),
    ("event", "SELECT * FROM events WHERE cluster_id = %(c)s ORDER BY id"),
)


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _line(kind: str, row: dict[str, Any]) -> str:
    if "kind" in row:  # a command's own kind column, so `kind` stays the record kind
        row = {("command_kind" if k == "kind" else k): v for k, v in row.items()}
    return json.dumps({"kind": kind, **row}, default=_default, separators=(",", ":")) + "\n"


async def export_cluster_history(
    conn: psycopg.AsyncConnection, cluster_id: uuid.UUID
) -> AsyncIterator[str]:
    """Yield the JSONL lines (newline-terminated) of a cluster's full history."""
    async with conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, slug, spec, created_at FROM clusters WHERE id = %s", (cluster_id,)
            )
            row = await cur.fetchone()
        if row is None:
            return
        yield _line("cluster", row)
        for kind, query in QUERIES:
            async with conn.cursor(name=f"export_{kind}", row_factory=dict_row) as cur:
                cur.itersize = FETCH_SIZE
                await cur.execute(query, {"c": cluster_id})
                async for row in cur:
                    yield _line(kind, row)
