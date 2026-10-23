"""Connection pool, NOTIFY helper and a reconnecting LISTEN stream."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import psycopg
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger(__name__)


async def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
    await pool.open(wait=True)
    return pool


async def notify(conn: psycopg.AsyncConnection, channel: str, payload: dict[str, Any]) -> None:
    await conn.execute("SELECT pg_notify(%s, %s)", (channel, json.dumps(payload)))


async def listen(
    dsn: str,
    channels: Sequence[str],
    *,
    initial_backoff: float = 0.5,
    max_backoff: float = 30.0,
) -> AsyncIterator[tuple[str, dict]]:
    """Yield (channel, payload) on a dedicated connection, reconnecting with backoff."""
    backoff = initial_backoff
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(dsn, autocommit=True) as conn:
                for ch in channels:
                    await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(ch)))
                backoff = initial_backoff
                async for n in conn.notifies():
                    try:
                        payload = json.loads(n.payload) if n.payload else {}
                    except ValueError:
                        payload = {"raw": n.payload}
                    yield n.channel, payload
        except (psycopg.OperationalError, OSError) as e:
            log.warning("listen connection lost (%s); retrying in %.1fs", e, backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)
