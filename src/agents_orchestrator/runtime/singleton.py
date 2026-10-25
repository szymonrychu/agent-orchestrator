"""Session-level advisory lock on a keepalive'd connection with a lock-held heartbeat (D20)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import psycopg

log = logging.getLogger(__name__)

EXIT_LOCK_LOST = 3

_HELD_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted"
    " AND pid = pg_backend_pid() AND objsubid = 1"
    " AND classid::bigint = %s AND objid::bigint = %s)"
)


def lock_key(name: str) -> int:
    """A stable signed 64-bit advisory-lock key for a singleton name."""
    digest = hashlib.sha256(f"agents-orchestrator:{name}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _halves(key: int) -> tuple[int, int]:
    unsigned = key & 0xFFFFFFFFFFFFFFFF
    return unsigned >> 32, unsigned & 0xFFFFFFFF


@dataclass
class Singleton:
    """The held lock. `lost` is set when the heartbeat or the connection gave it up."""

    name: str
    key: int
    stop: asyncio.Event
    conn: psycopg.AsyncConnection | None = None
    held: bool = False
    lost: bool = False
    _hb: asyncio.Task[None] | None = field(default=None, repr=False)


async def _held(conn: psycopg.AsyncConnection, classid: int, objid: int) -> bool:
    cur = await conn.execute(_HELD_SQL, (classid, objid))
    row = await cur.fetchone()
    return bool(row and row[0])


async def _heartbeat(sing: Singleton, interval: float, timeout: float) -> None:
    assert sing.conn is not None
    classid, objid = _halves(sing.key)
    while not sing.stop.is_set():
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(sing.stop.wait(), interval)
        if sing.stop.is_set():
            return
        # Not asyncio.timeout: cancelling a query on a dead socket makes psycopg wait out its
        # own cancellation timeout. The query is abandoned; closing the connection ends it.
        query = asyncio.create_task(_held(sing.conn, classid, objid))
        query.add_done_callback(lambda t: t.cancelled() or t.exception())
        try:
            done, _ = await asyncio.wait({query}, timeout=timeout)
            if not done:
                raise TimeoutError(f"no answer within {timeout}s")
            ok = query.result()
        except (psycopg.Error, OSError, TimeoutError) as exc:
            log.error("singleton %s heartbeat failed: %s: %s", sing.name, type(exc).__name__, exc)
            ok = False
        if not ok:
            log.error("singleton %s lost the advisory lock; stopping", sing.name)
            sing.lost = True
            sing.held = False
            sing.stop.set()
            return


async def _close(conn: psycopg.AsyncConnection) -> None:
    with contextlib.suppress(Exception):
        async with asyncio.timeout(2):
            await conn.close()


@contextlib.asynccontextmanager
async def advisory_singleton(
    dsn: str,
    name: str,
    *,
    heartbeat_seconds: float,
    tcp_user_timeout_seconds: float,
    stop: asyncio.Event | None = None,
) -> AsyncIterator[Singleton]:
    """Hold pg_advisory_lock(name) on a dedicated connection, blocking until acquired.

    A second replica waits here as a hot standby. If `stop` is set while waiting, the
    context yields with `held == False`. Once held, a heartbeat every `heartbeat_seconds`
    checks that this backend still owns the lock; a failed, timed-out or negative check, or a
    dropped connection, sets `lost` and `stop` so the caller exits with EXIT_LOCK_LOST.
    """
    stop = stop if stop is not None else asyncio.Event()
    key = lock_key(name)
    sing = Singleton(name=name, key=key, stop=stop)
    third = max(1, int(tcp_user_timeout_seconds / 3))
    conn = await psycopg.AsyncConnection.connect(
        dsn,
        autocommit=True,
        connect_timeout=max(1, int(tcp_user_timeout_seconds)),
        keepalives=1,
        keepalives_idle=third,
        keepalives_interval=third,
        keepalives_count=3,
        tcp_user_timeout=int(tcp_user_timeout_seconds * 1000),
    )
    sing.conn = conn
    try:
        acquire = asyncio.create_task(conn.execute("SELECT pg_advisory_lock(%s)", (key,)))
        stop_wait = asyncio.create_task(stop.wait())
        try:
            await asyncio.wait({acquire, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_wait.cancel()
        if acquire.done():
            acquire.result()
            sing.held = True
            log.info("singleton %s acquired", name)
            sing._hb = asyncio.create_task(
                _heartbeat(sing, heartbeat_seconds, tcp_user_timeout_seconds)
            )
        else:
            acquire.cancel()
            with contextlib.suppress(BaseException):
                await acquire
        yield sing
    finally:
        if sing._hb is not None:
            sing._hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sing._hb
        await _close(conn)
