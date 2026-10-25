"""Event paging and the two SSE streams (D11, D14)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from agents_orchestrator.api.clusters import Cfg, Pool, agent_view
from agents_orchestrator.api.clusters import _view as cluster_view
from agents_orchestrator.api.errors import ApiError, not_found
from agents_orchestrator.api.listener import Listener, Subscription, SubscriptionDropped
from agents_orchestrator.api.messages import _DEFAULT_ATTEMPTS, _message_view
from agents_orchestrator.db import clusters as repo
from agents_orchestrator.db import events as ev
from agents_orchestrator.db import views

router = APIRouter(prefix="/api/v1")

DEFAULT_POLL_SECONDS = 5.0
_STREAM_PAGE = ev.MAX_LIMIT
_RECENT_MESSAGES = 100


class EventView(BaseModel):
    id: int
    cluster_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: str
    seq: int
    ts: datetime
    type: str
    turn_id: str | None
    data: Any


class EventsPage(BaseModel):
    events: list[EventView]
    has_more_before: bool
    next_after_id: int | None


def get_listener(request: Request) -> Listener:
    return request.app.state.listener


def get_poll_seconds(request: Request) -> float:
    return request.app.state.stream_poll_seconds


Notifier = Annotated[Listener, Depends(get_listener)]
PollSeconds = Annotated[float, Depends(get_poll_seconds)]


def _bad_request(message: str) -> ApiError:
    return ApiError(400, "bad_request", message)


def _split_types(types: list[str] | None) -> list[str] | None:
    out = [t for raw in types or [] for t in raw.split(",") if t]
    return out or None


@router.get("/clusters/{cluster_id}/events", response_model=EventsPage)
async def list_events(
    cluster_id: uuid.UUID,
    pool: Pool,
    before_id: Annotated[int | None, Query(ge=0)] = None,
    after_id: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=ev.MAX_LIMIT)] = 200,
    agent_id: uuid.UUID | None = None,
    types: Annotated[list[str] | None, Query()] = None,
) -> EventsPage:
    if before_id is not None and after_id is not None:
        raise _bad_request("before_id and after_id are mutually exclusive")
    async with pool.connection() as conn:
        if await repo.get(conn, cluster_id) is None:
            raise not_found("cluster")
        page = await ev.page(
            conn,
            cluster_id,
            before_id=before_id,
            after_id=after_id,
            limit=limit,
            agent_id=agent_id,
            types=_split_types(types),
        )
    return EventsPage(
        events=[EventView(**e) for e in page.events],
        has_more_before=page.has_more_before,
        next_after_id=page.next_after_id,
    )


async def resume_cursor(
    cluster_id: uuid.UUID,
    pool: Pool,
    after_id: Annotated[int | None, Query(ge=0)] = None,
    last_event_id: Annotated[str | None, Header()] = None,
) -> tuple[uuid.UUID, int]:
    """Validate the cluster and resolve where the stream starts (before any byte is sent)."""
    cursor = after_id
    if cursor is None and last_event_id not in (None, ""):
        try:
            cursor = int(last_event_id)  # type: ignore[arg-type]
        except ValueError:
            raise _bad_request("Last-Event-ID must be an integer event id") from None
    async with pool.connection() as conn:
        if await repo.get(conn, cluster_id) is None:
            raise not_found("cluster")
        if cursor is None:
            cur = await conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM events WHERE cluster_id = %s", (cluster_id,)
            )
            cursor = (await cur.fetchone())[0]
    return cluster_id, cursor


def _matches(payload: dict, cluster_id: uuid.UUID) -> bool:
    return payload.get("cluster_id") == str(cluster_id)


async def _wait_for_cluster(sub: Subscription, cluster_id: uuid.UUID, poll: float) -> bool:
    """Wait for a NOTIFY about this cluster or for the poll interval; other clusters are ignored."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + poll
    while (remaining := deadline - loop.time()) > 0:
        if await sub.wait(remaining) and any(_matches(p, cluster_id) for _, p in sub.drain()):
            return True
    return False


@router.get("/clusters/{cluster_id}/events/stream", response_class=EventSourceResponse)
async def stream_events(
    start: Annotated[tuple[uuid.UUID, int], Depends(resume_cursor)],
    pool: Pool,
    listener: Notifier,
    poll: PollSeconds,
) -> AsyncIterator[ServerSentEvent]:
    cluster_id, cursor = start
    sub = listener.subscribe(["events"])
    try:
        while True:
            while True:  # replay stored rows after the cursor, page by page
                async with pool.connection() as conn:
                    page = await ev.page(conn, cluster_id, after_id=cursor, limit=_STREAM_PAGE)
                for row in page.events:
                    yield ServerSentEvent(
                        data=EventView(**row), event=row["type"], id=str(row["id"])
                    )
                if page.events:
                    cursor = page.events[-1]["id"]
                if len(page.events) < _STREAM_PAGE:
                    break
            try:
                if not await _wait_for_cluster(sub, cluster_id, poll):
                    continue
            except SubscriptionDropped:
                return
    finally:
        sub.close()


# ------------------------------------------------------------------ status stream


async def _cluster_state(conn, cluster: repo.Cluster, settings) -> dict[str, Any]:
    aggs = await views.cluster_aggregates(conn, cluster.id)
    agg = aggs.get(cluster.id) or {}
    rows = await views.agent_rows(conn, cluster.id)
    msgs = await views.list_messages(conn, cluster.id, None, _RECENT_MESSAGES, _DEFAULT_ATTEMPTS)
    return {
        "cluster": cluster_view(cluster, agg, settings).model_dump(mode="json"),
        "agents": {str(r["id"]): agent_view(r).model_dump(mode="json") for r in rows},
        "messages": {m["id"]: _message_view(m).model_dump(mode="json") for m in msgs},
        "queued": agg.get("queued", 0),
        "failed": agg.get("failed", 0),
    }


def _snapshot_entry(state: dict[str, Any]) -> dict[str, Any]:
    return {"cluster": state["cluster"], "agents": list(state["agents"].values())}


def _deltas(cid: str, old: dict[str, Any] | None, new: dict[str, Any]) -> list[ServerSentEvent]:
    out: list[ServerSentEvent] = []
    if old is None or old["cluster"] != new["cluster"]:
        out.append(ServerSentEvent(data={"cluster": new["cluster"]}, event="cluster_status"))
    old_agents = old["agents"] if old else {}
    for aid, agent in new["agents"].items():
        if old_agents.get(aid) != agent:
            out.append(
                ServerSentEvent(data={"cluster_id": cid, "agent": agent}, event="agent_status")
            )
    old_msgs = old["messages"] if old else {}
    changed = [m for mid, m in new["messages"].items() if old_msgs.get(mid) != m]
    counts_changed = old is None or (old["queued"], old["failed"]) != (new["queued"], new["failed"])
    if changed or counts_changed:
        out.append(
            ServerSentEvent(
                data={
                    "cluster_id": cid,
                    "queued": new["queued"],
                    "failed": new["failed"],
                    "messages": changed,
                },
                event="message_status",
            )
        )
    return out


async def _refresh(
    pool: AsyncConnectionPool,
    settings,
    known: dict[str, dict[str, Any]],
    only: set[str] | None,
) -> list[ServerSentEvent]:
    """Re-read the touched clusters (all when `only` is None) and emit what changed."""
    out: list[ServerSentEvent] = []
    async with pool.connection() as conn:
        clusters = {str(c.id): c for c in await repo.list_clusters(conn)}
        for cid in list(known):
            if cid not in clusters and (only is None or cid in only):
                del known[cid]
                out.append(ServerSentEvent(data={"cluster_id": cid}, event="cluster_removed"))
        for cid, cluster in clusters.items():
            if only is not None and cid not in only:
                continue
            new = await _cluster_state(conn, cluster, settings)
            out.extend(_deltas(cid, known.get(cid), new))
            known[cid] = new
    return out


@router.get("/stream", response_class=EventSourceResponse)
async def stream_status(
    pool: Pool, settings: Cfg, listener: Notifier, poll: PollSeconds
) -> AsyncIterator[ServerSentEvent]:
    sub: Subscription = listener.subscribe(["status", "cluster_changed", "leader_changed"])
    try:
        known: dict[str, dict[str, Any]] = {}
        async with pool.connection() as conn:
            for cluster in await repo.list_clusters(conn):
                known[str(cluster.id)] = await _cluster_state(conn, cluster, settings)
        yield ServerSentEvent(
            data={"clusters": [_snapshot_entry(s) for s in known.values()]}, event="snapshot"
        )
        while True:
            try:
                notified = await sub.wait(poll)
                drained = sub.drain()
            except SubscriptionDropped:
                return
            touched = {p["cluster_id"] for _, p in drained if "cluster_id" in p}
            for channel, payload in drained:
                if channel == "leader_changed":
                    yield ServerSentEvent(
                        data={k: payload.get(k) for k in ("cluster_id", "agent_id", "epoch")},
                        event="leader_changed",
                    )
            # a NOTIFY refreshes its cluster; a timeout is the poll and refreshes everything
            for event in await _refresh(
                pool, settings, known, (touched or None) if notified else None
            ):
                yield event
    finally:
        sub.close()
