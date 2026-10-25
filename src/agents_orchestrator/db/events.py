"""Repository for the `events` table: idempotent insert, gap rows and keyset paging."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from agents_orchestrator.db.pool import notify

MAX_LIMIT = 500
CHANNEL = "events"
_COLUMNS = "id, session_id, agent_id, wrapper_session_id, seq, ts, type, turn_id, data"


@dataclass
class Page:
    events: list[dict[str, Any]] = field(default_factory=list)
    has_more_before: bool = False
    next_after_id: int | None = None


async def _notify_new(conn: psycopg.AsyncConnection, session_id: uuid.UUID, max_id: int) -> None:
    # ids only, never content: consumers re-read from the table
    await notify(conn, CHANNEL, {"session_id": str(session_id), "max_id": max_id})


async def insert_events(
    conn: psycopg.AsyncConnection,
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    wrapper_session_id: str,
    envelopes: Sequence[dict[str, Any]],
) -> int | None:
    """Insert wrapper envelopes {seq, ts, type, data}; duplicates are ignored.

    Returns the max id of the rows that were new, or None when nothing was new.
    NOTIFYs `events {session_id, max_id}` only in the former case.
    """
    max_id: int | None = None
    for e in envelopes:
        data = e.get("data") or {}
        turn_id = data.get("turn_id") if isinstance(data, dict) else None
        cur = await conn.execute(
            "INSERT INTO events"
            " (session_id, agent_id, wrapper_session_id, seq, ts, type, turn_id, data)"
            " VALUES (%s,%s,%s,%s,COALESCE(%s::timestamptz, now()),%s,%s,%s)"
            " ON CONFLICT (agent_id, wrapper_session_id, seq) DO NOTHING RETURNING id",
            (
                session_id,
                agent_id,
                wrapper_session_id,
                e["seq"],
                e.get("ts"),
                e["type"],
                turn_id,
                Jsonb(data),
            ),
        )
        row = await cur.fetchone()
        if row is not None:
            max_id = row[0] if max_id is None else max(max_id, row[0])
    if max_id is not None:
        await _notify_new(conn, session_id, max_id)
    return max_id


async def insert_gap(
    conn: psycopg.AsyncConnection,
    session_id: uuid.UUID,
    agent_id: uuid.UUID,
    wrapper_session_id: str,
    after_seq: int,
    before_seq: int,
) -> int | None:
    """Record a missing seq range (after_seq, before_seq) as a synthetic `gap` row.

    Stored under seq = -before_seq so it never collides with a real wrapper seq
    and is ignored by last_seq. Returns the new id (None if the gap already exists).
    """
    cur = await conn.execute(
        "INSERT INTO events (session_id, agent_id, wrapper_session_id, seq, type, data)"
        " VALUES (%s,%s,%s,%s,'gap',%s)"
        " ON CONFLICT (agent_id, wrapper_session_id, seq) DO NOTHING RETURNING id",
        (
            session_id,
            agent_id,
            wrapper_session_id,
            -before_seq,
            Jsonb({"after_seq": after_seq, "before_seq": before_seq}),
        ),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    await _notify_new(conn, session_id, row[0])
    return row[0]


async def last_seq(
    conn: psycopg.AsyncConnection, agent_id: uuid.UUID, wrapper_session_id: str
) -> int:
    cur = await conn.execute(
        "SELECT COALESCE(MAX(seq), 0) FROM events WHERE agent_id=%s AND wrapper_session_id=%s"
        " AND type <> 'gap'",
        (agent_id, wrapper_session_id),
    )
    return (await cur.fetchone())[0]


def _row(row: tuple) -> dict[str, Any]:
    keys = (
        "id",
        "session_id",
        "agent_id",
        "wrapper_session_id",
        "seq",
        "ts",
        "type",
        "turn_id",
        "data",
    )
    return dict(zip(keys, row, strict=True))


async def page(
    conn: psycopg.AsyncConnection,
    session_id: uuid.UUID,
    before_id: int | None = None,
    after_id: int | None = None,
    limit: int = 200,
    agent_id: uuid.UUID | None = None,
    types: Sequence[str] | None = None,
) -> Page:
    """One page of a session's merged timeline, always in ascending id order.

    No cursor: the newest `limit` rows. before_id: the `limit` rows just older.
    after_id: the `limit` rows just newer. limit is capped at 500.
    """
    if before_id is not None and after_id is not None:
        raise ValueError("before_id and after_id are mutually exclusive")
    limit = max(1, min(limit, MAX_LIMIT))

    where = ["session_id = %s"]
    params: list[Any] = [session_id]
    if agent_id is not None:
        where.append("agent_id = %s")
        params.append(agent_id)
    if types:
        where.append("type = ANY(%s)")
        params.append(list(types))
    base = " AND ".join(where)

    if after_id is not None:
        cur = await conn.execute(
            f"SELECT {_COLUMNS} FROM events WHERE {base} AND id > %s ORDER BY id ASC LIMIT %s",
            (*params, after_id, limit),
        )
        events = [_row(r) for r in await cur.fetchall()]
        has_more_before = False
        if events:
            cur = await conn.execute(
                f"SELECT EXISTS (SELECT 1 FROM events WHERE {base} AND id < %s)",
                (*params, events[0]["id"]),
            )
            has_more_before = (await cur.fetchone())[0]
        return Page(events, has_more_before, events[-1]["id"] if events else after_id)

    cursor_sql, cursor_params = ("", [])
    if before_id is not None:
        cursor_sql, cursor_params = " AND id < %s", [before_id]
    cur = await conn.execute(
        f"SELECT {_COLUMNS} FROM events WHERE {base}{cursor_sql} ORDER BY id DESC LIMIT %s",
        (*params, *cursor_params, limit + 1),
    )
    rows = await cur.fetchall()
    has_more = len(rows) > limit
    events = [_row(r) for r in reversed(rows[:limit])]
    return Page(events, has_more, events[-1]["id"] if events else None)
