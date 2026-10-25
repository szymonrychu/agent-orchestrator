"""Hive state and activity feed push (D3, D9, D23).

`push_once` runs in the dispatcher process only, never in the singleton ingester. For each hive
session it takes the session's advisory lock (try; skip when held) around the DB read, releases
it, pushes notes to the agents' wrappers with no transaction open, then takes the lock again
around the DB write. Pushes are best effort: a wrapper that is down, stopped or hanging costs
that agent its note for this tick and nothing else.

State kept here:
- `session_leaders.state_hash` is a JSON map `{agent_id: hash}` of the hive state each agent
  last accepted, so one failing agent never causes re-pushes to the others. The dispatcher is
  the only writer. `state_pushed_at` moves only when every receiver took the current state.
- `session_leaders.feed_cursor` is the last event id whose activity was handed out. Event ids
  commit out of order across the ingester's per-agent transactions, so only rows older than
  FEED_SETTLE_SECONDS are consumed, and never past the first newer one. Bash calls still
  awaiting a result are carried in memory (`HiveState`) and hold the stored cursor back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from agents_orchestrator.db import leadership
from agents_orchestrator.db.queue import _CLAIM_LOCK_NAMESPACE
from agents_orchestrator.dispatcher import feed
from agents_orchestrator.settings import Settings
from agents_orchestrator.wrapper.client import NoteOutcome, WrapperClient

log = logging.getLogger(__name__)

# Module constants by design (D14).
PUSH_INTERVAL_SECONDS = 10
PUSH_TIMEOUT_SECONDS = 3
FEED_SETTLE_SECONDS = 5
FEED_BATCH = 500
# Under the wrapper's default max_note_chars (2000).
FEED_MAX_CHARS = 1800
STATE_KEY = "hive_state"
HANDOVER_MARGIN_PCT = 15
_FEED_TYPES = ("tool_call", "tool_result", "turn_completed")

ClientFactory = Callable[[uuid.UUID, str], WrapperClient]


@dataclass
class HiveState:
    """In-memory push state of one dispatcher: throttle clock and feed carry-over per session."""

    last_tick: dict[uuid.UUID, float] = field(default_factory=dict)
    watermark: dict[uuid.UUID, int] = field(default_factory=dict)
    carried: dict[uuid.UUID, list[dict[str, Any]]] = field(default_factory=dict)


# ---------------------------------------------------------------- the note text


def hive_state_payload(
    you: str,
    leader: str,
    epoch: int,
    members: list[dict[str, Any]],
    max_replicas: int,
    static: bool,
) -> dict[str, Any]:
    """The hive_state schema: leader, epoch, you, members, max_replicas, static."""
    return {
        "leader": leader,
        "epoch": epoch,
        "you": you,
        "members": [
            {
                "pod_name": m["pod_name"],
                "ready": m["ready"],
                "state": m["state"],
                "context_pct": m["context_pct"],
                "leader": m["pod_name"] == leader,
            }
            for m in members
        ],
        "max_replicas": max_replicas,
        "static": static,
    }


def render_hive_state(payload: dict[str, Any], slug: str, auto_compact_pct: int | None) -> str:
    """The `<hive-state>` text block pushed under the key `hive_state`."""
    clean = feed._clean
    role = "leader" if payload["you"] == payload["leader"] else "member"
    members = []
    for m in payload["members"]:
        parts = ["leader" if m["leader"] else "member", "ready" if m["ready"] else "not ready"]
        parts.append(clean(m["state"]))
        if m["context_pct"] is not None:
            parts.append(f"{round(m['context_pct'])}% context")
        members.append(f"{clean(m['pod_name'])} ({', '.join(parts)})")
    lines = [
        "<hive-state>",
        f"You are {clean(payload['you'])} ({role}) in hive {clean(slug)} of up to "
        f"{payload['max_replicas']} agents. Leader: {clean(payload['leader'])} "
        f"(epoch {payload['epoch']}). Members: {', '.join(members)}."
        + (" The hive is static: agents are not added or removed." if payload["static"] else ""),
    ]
    if auto_compact_pct is not None:
        threshold = max(auto_compact_pct - HANDOVER_MARGIN_PCT, 1)
        lines.append(f"Hand over leadership instead of compacting above {threshold}% context.")
    lines.append("</hive-state>")
    return "\n".join(lines)


def state_hash(leader: str, epoch: int, members: list[dict[str, Any]]) -> str:
    """Changes with leader, epoch, member set, wrapper sessions, ready, state, 10% context."""
    pct = [None if m["context_pct"] is None else int(m["context_pct"] // 10) for m in members]
    key = [
        leader,
        epoch,
        [
            [m["pod_name"], m["ready"], m["state"], bucket, m["wrapper_session_id"]]
            for m, bucket in zip(members, pct, strict=True)
        ],
    ]
    return hashlib.sha256(json.dumps(key).encode()).hexdigest()


# ---------------------------------------------------------------- one session, three phases


@dataclass
class _Plan:
    session_id: uuid.UUID
    slug: str
    auto_compact_pct: int | None
    max_replicas: int
    static: bool
    leader: str
    epoch: int
    feed_cursor: int
    new_cursor: int
    hashes: dict[str, str]  # per agent, the state hash it last accepted
    members: list[dict[str, Any]]
    lines: list[feed.FeedLine]

    @property
    def receivers(self) -> list[dict[str, Any]]:
        return [m for m in self.members if m["url"] and m["wrapper_session_id"]]


async def _try_lock(conn: psycopg.AsyncConnection, session_id: uuid.UUID) -> bool:
    cur = await conn.execute(
        "SELECT pg_try_advisory_xact_lock(%s, hashtext(%s::text))",
        (_CLAIM_LOCK_NAMESPACE, session_id),
    )
    row = await cur.fetchone()
    return bool(row and row[0])


async def _read(
    conn: psycopg.AsyncConnection, session: dict[str, Any], state: HiveState
) -> _Plan | None:
    """Phase 1: under the lock, read the hive and consume the settled feed rows."""
    cid = session["id"]
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT a.pod_name AS leader, l.epoch, l.feed_cursor, l.state_hash"
            " FROM session_leaders l JOIN agents a ON a.id = l.agent_id WHERE l.session_id = %s",
            (cid,),
        )
        leader = await cur.fetchone()
        if leader is None:
            return None
        await cur.execute(
            "SELECT a.id, a.pod_name, a.url, a.wrapper_session_id, a.ready,"
            " coalesce(s.state, 'unknown') AS state, s.context_pct"
            " FROM agents a LEFT JOIN agent_status s ON s.agent_id = a.id"
            " WHERE a.session_id = %s AND a.gone_at IS NULL ORDER BY a.ordinal",
            (cid,),
        )
        members = await cur.fetchall()
        cursor = leader["feed_cursor"]
        start = max(state.watermark.get(cid, 0), cursor)
        await cur.execute(
            "SELECT e.id, e.type, e.data, e.ts, coalesce(a.pod_name, 'agent') AS agent,"
            " e.created_at <= now() - make_interval(secs => %s) AS settled"
            " FROM events e LEFT JOIN agents a ON a.id = e.agent_id"
            " WHERE e.session_id = %s AND e.id > %s AND e.type = ANY(%s)"
            " ORDER BY e.id LIMIT %s",
            (FEED_SETTLE_SECONDS, cid, start, list(_FEED_TYPES), FEED_BATCH),
        )
        fetched = await cur.fetchall()
    rows = []
    for row in fetched:  # never past the first row that may still have a lower id in flight
        if not row.pop("settled"):
            break
        rows.append(row)
    carried = state.carried.get(cid, [])
    lines, pending = feed.digest_lines([*carried, *rows], datetime.now(UTC))
    watermark = rows[-1]["id"] if rows else start
    state.watermark[cid] = watermark
    state.carried[cid] = [r for r in [*carried, *rows] if r["id"] in set(pending)]
    new_cursor = min([watermark, *(p - 1 for p in pending)])
    spec = session["spec"]
    return _Plan(
        session_id=cid,
        slug=session["slug"],
        auto_compact_pct=spec.get("auto_compact_pct"),
        max_replicas=spec.get("max_replicas") or spec.get("replicas") or 1,
        static=bool(spec.get("static")),
        leader=leader["leader"],
        epoch=leader["epoch"],
        feed_cursor=cursor,
        new_cursor=max(new_cursor, cursor),
        hashes=_load_hashes(leader["state_hash"]),
        members=members,
        lines=lines,
    )


def _load_hashes(raw: str | None) -> dict[str, str]:
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _note(
    client: WrapperClient, wrapper_session_id: str, text: str, key: str | None = None
) -> NoteOutcome:
    try:
        async with asyncio.timeout(PUSH_TIMEOUT_SECONDS):
            return await client.post_note(
                wrapper_session_id, text, key=key, timeout=PUSH_TIMEOUT_SECONDS
            )
    except TimeoutError:
        log.info("note to session %s timed out", wrapper_session_id)
        return "failed"
    except Exception:
        log.exception("note to session %s failed", wrapper_session_id)
        return "failed"


async def _push_agent(
    plan: _Plan, member: dict[str, Any], due: bool, client_factory: ClientFactory
) -> tuple[NoteOutcome | None, int]:
    """(outcome of the hive_state push or None when it was not due, notes accepted)."""
    state_outcome: NoteOutcome | None = None
    sent = 0
    client = client_factory(plan.session_id, member["url"])
    if due:
        payload = hive_state_payload(
            member["pod_name"],
            plan.leader,
            plan.epoch,
            plan.members,
            plan.max_replicas,
            plan.static,
        )
        text = render_hive_state(payload, plan.slug, plan.auto_compact_pct)
        state_outcome = await _note(client, member["wrapper_session_id"], text, STATE_KEY)
        sent += state_outcome == "ok"
        if state_outcome != "ok":
            return state_outcome, sent  # no point trying the feed on a wrapper that said no
    text = feed.render_feed(plan.lines, member["pod_name"], max_chars=FEED_MAX_CHARS)
    if text:
        sent += await _note(client, member["wrapper_session_id"], text) == "ok"
    return state_outcome, sent


async def _claim(conn: psycopg.AsyncConnection, plan: _Plan, current: str) -> list[dict[str, Any]]:
    """Under the lock, before any push: record the work as taken, so a second dispatcher that
    reads while this one is on the wire finds nothing left to push.

    Advances the feed cursor and marks every receiver whose state is not current as holding
    `current`; returns those receivers (the ones to push to). A failed push is undone in
    `_release`; a failed feed push is not retried.
    """
    live = {str(m["id"]) for m in plan.members}
    due = [m for m in plan.receivers if plan.hashes.get(str(m["id"])) != current]
    hashes = {k: v for k, v in plan.hashes.items() if k in live}
    hashes.update({str(m["id"]): current for m in due})
    await conn.execute(
        "UPDATE session_leaders SET state_hash = %s, feed_cursor = %s WHERE session_id = %s",
        (json.dumps(hashes), plan.new_cursor, plan.session_id),
    )
    return due


async def _release(
    pool: AsyncConnectionPool, plan: _Plan, current: str, failed: set[str], all_ok: bool
) -> None:
    """After the pushes: take back the claim of failed receivers (they retry alone on the next
    tick) and stamp `state_pushed_at` when every push of this tick was taken."""
    async with pool.connection() as conn, conn.transaction():
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT state_hash FROM session_leaders WHERE session_id = %s FOR UPDATE",
                (plan.session_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return
        hashes = _load_hashes(row["state_hash"])
        for agent_id in failed:
            if hashes.get(agent_id) == current:
                del hashes[agent_id]
        await conn.execute(
            "UPDATE session_leaders SET state_hash = %s,"
            " state_pushed_at = CASE WHEN %s THEN now() ELSE state_pushed_at END"
            " WHERE session_id = %s",
            (json.dumps(hashes), all_ok, plan.session_id),
        )


async def _push_session(
    pool: AsyncConnectionPool,
    session: dict[str, Any],
    client_factory: ClientFactory,
    state: HiveState,
    clock: Callable[[], float],
) -> int:
    cid = session["id"]
    async with pool.connection() as conn, conn.transaction():
        if not await _try_lock(conn, cid):
            return 0
        state.last_tick[cid] = clock()
        plan = await _read(conn, session, state)
        if plan is None:
            return 0
        current = state_hash(plan.leader, plan.epoch, plan.members)
        due = {str(m["id"]) for m in await _claim(conn, plan, current)}
    receivers = plan.receivers
    results = await asyncio.gather(
        *(_push_agent(plan, m, str(m["id"]) in due, client_factory) for m in receivers)
    )
    sent = sum(n for _outcome, n in results)
    # disabled counts as taken (nothing to retry)
    failed = {
        str(m["id"])
        for m, (outcome, _n) in zip(receivers, results, strict=True)
        if outcome == "failed"
    }
    if due:
        await _release(pool, plan, current, failed, all_ok=not failed)
    return sent


async def push_once(
    pool: AsyncConnectionPool,
    settings: Settings,
    client_factory: ClientFactory,
    state: HiveState | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """One push pass over every hive session; returns the number of notes accepted.

    A session is evaluated at most once per PUSH_INTERVAL_SECONDS (by `clock`). One failing
    session is logged and never holds up the others.
    """
    state = state if state is not None else HiveState()
    async with pool.connection() as conn:
        sessions = await leadership.hive_sessions(conn)
    total = 0
    for session in sessions:
        last = state.last_tick.get(session["id"])
        if last is not None and clock() - last < PUSH_INTERVAL_SECONDS:
            continue
        try:
            total += await _push_session(pool, session, client_factory, state, clock)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("hive push for session %s failed", session["id"])
    return total
