"""Agent API (`/agent/v1`), served only on the agent port (see allowlist.PortGate).

`require_agent()` is the identity contract of every agent route: a cluster token plus the
direct socket peer IP bound to a live agent pod. No forwarded header is read (D22).
"""

from __future__ import annotations

import hmac
import posixpath
import uuid
from datetime import datetime
from typing import Annotated, Any
from xml.sax.saxutils import escape

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field

from agents_orchestrator.api import clusters as clusters_api
from agents_orchestrator.api import schemas
from agents_orchestrator.api.clusters import Cfg, Pool, edit_init_scripts, init_scripts_view
from agents_orchestrator.api.errors import ApiError
from agents_orchestrator.db import agents as agents_repo
from agents_orchestrator.db import clusters as clusters_repo
from agents_orchestrator.db import file_locks, leadership
from agents_orchestrator.spec import WORKDIR_ROOT
from agents_orchestrator.tokens import tokens_for

router = APIRouter(prefix="/agent/v1", tags=["agent"])

_UNAUTHORIZED = "missing or invalid agent credentials"


class Member(BaseModel):
    pod_name: str
    ready: bool
    state: str
    context_pct: float | None
    leader: bool


class Me(BaseModel):
    cluster_id: uuid.UUID
    agent_id: uuid.UUID
    pod_name: str
    ordinal: int
    leader: str | None
    epoch: int | None
    max_replicas: int
    static: bool
    members: list[Member]


def _unauthorized() -> ApiError:
    return ApiError(401, "unauthorized", _UNAUTHORIZED)


async def require_agent(
    request: Request,
    pool: Pool,
    settings: Cfg,
    authorization: Annotated[str | None, Header()] = None,
    x_orch_cluster: Annotated[str | None, Header()] = None,
) -> agents_repo.Agent:
    """The calling agent's row. 401 for any credential problem (one shared answer), 403 when
    the socket peer is not the pod IP of a live agent of that cluster."""
    scheme, _, presented = (authorization or "").partition(" ")
    try:
        cluster_id = uuid.UUID(x_orch_cluster or "")
    except ValueError:
        raise _unauthorized() from None
    if scheme.lower() != "bearer" or not presented:
        raise _unauthorized()
    async with pool.connection() as conn:
        cluster = await clusters_repo.get(conn, cluster_id)
        expected = tokens_for(settings, cluster_id).agent
        if cluster is None or not hmac.compare_digest(presented.encode(), expected.encode()):
            raise _unauthorized()
        client = request.scope.get("client")
        agent = await agents_repo.by_pod_ip(conn, cluster_id, client[0]) if client else None
    if agent is None:
        raise ApiError(403, "forbidden", "caller is not a live agent pod of this cluster")
    return agent


Caller = Annotated[agents_repo.Agent, Depends(require_agent)]


@router.get("/me", response_model=Me, summary="Identity, leader and members of the caller")
async def me(agent: Caller, pool: Pool) -> Me:
    async with pool.connection() as conn:
        cluster = await clusters_repo.get(conn, agent.cluster_id)
        leader = await agents_repo.leader_of(conn, agent.cluster_id)
        members = await agents_repo.list_members(conn, agent.cluster_id)
    assert cluster is not None
    leader_id = leader[0] if leader else None
    leader_pod = next((m["pod_name"] for m in members if m["id"] == leader_id), None)
    return Me(
        cluster_id=agent.cluster_id,
        agent_id=agent.id,
        pod_name=agent.pod_name,
        ordinal=agent.ordinal,
        leader=leader_pod,
        epoch=leader[1] if leader else None,
        max_replicas=cluster.spec.max_replicas or cluster.spec.replicas,
        static=cluster.spec.static,
        members=[
            Member(
                pod_name=m["pod_name"],
                ready=m["ready"],
                state=m["state"],
                context_pct=m["context_pct"],
                leader=m["id"] == leader_id,
            )
            for m in members
        ],
    )


_SCALE_STATUS = {**clusters_api.SCALE_STATUS, "not_leader": 403}


@router.post(
    "/scale",
    status_code=202,
    response_model=schemas.ScaleResult,
    summary="Scale the caller's cluster (leader only)",
)
async def scale(agent: Caller, body: schemas.ScaleRequest, pool: Pool) -> schemas.ScaleResult:
    async with pool.connection() as conn:
        try:
            out = await clusters_repo.set_replicas(
                conn, agent.cluster_id, body.replicas, caller_agent_id=agent.id
            )
        except clusters_repo.ScaleRefused as exc:
            raise ApiError(_SCALE_STATUS.get(exc.code, 409), exc.code, str(exc)) from None
    return schemas.ScaleResult(
        cluster_id=out.cluster_id,
        replicas=out.replicas,
        previous_replicas=out.previous_replicas,
        max_replicas=out.max_replicas,
    )


@router.get(
    "/init-scripts",
    response_model=schemas.InitScriptsView,
    summary="Init scripts and last init run of the caller's cluster",
)
async def init_scripts_get(agent: Caller, pool: Pool, settings: Cfg) -> schemas.InitScriptsView:
    async with pool.connection() as conn:
        cluster = await clusters_repo.get(conn, agent.cluster_id)
        assert cluster is not None
        return init_scripts_view(conn, cluster, settings)


@router.put(
    "/init-scripts",
    response_model=schemas.InitScriptsView,
    summary="Replace the init scripts of the caller's cluster and its source preset",
)
async def init_scripts_put(
    body: schemas.InitScriptsIn, agent: Caller, pool: Pool, settings: Cfg
) -> schemas.InitScriptsView:
    async with pool.connection() as conn:
        return await edit_init_scripts(conn, agent.cluster_id, body.scripts, settings)


@router.post(
    "/leadership/handover",
    status_code=202,
    response_model=schemas.LeadershipRequested,
    summary="Hand leadership over (leader only)",
)
async def handover(
    body: schemas.HandoverIn, agent: Caller, pool: Pool
) -> schemas.LeadershipRequested:
    """Queue a handover. The checks here fail fast; the dispatcher re-checks at apply time."""
    async with pool.connection() as conn:
        cluster = await clusters_repo.get(conn, agent.cluster_id)
        assert cluster is not None
        if cluster.spec.alone:
            raise ApiError(409, "alone", "a single-agent cluster has no leader to change")
        leader = await leadership.current_leader(conn, agent.cluster_id)
        if leader is None or leader.agent_id != agent.id:
            raise ApiError(409, "not_leader", "only the current leader can hand leadership over")
        if body.epoch != leader.epoch:
            raise ApiError(
                409, "stale_epoch", f"epoch {body.epoch} is not the current epoch {leader.epoch}"
            )
        to_id = None
        if body.to is not None:
            member = next(
                (
                    m
                    for m in await agents_repo.list_members(conn, agent.cluster_id)
                    if m["pod_name"] == body.to
                ),
                None,
            )
            if member is None:
                raise ApiError(404, "not_found", f"agent {body.to} not found in this cluster")
            to_id = member["id"]
        request_id = await leadership.insert_request(
            conn, agent.cluster_id, "handover", agent.id, to_id, body.reason, body.epoch
        )
    return schemas.LeadershipRequested(request_id=request_id)


class LockPaths(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(min_length=1, max_length=50)


class LockHeld(BaseModel):
    path: str
    agent: str


class LocksClaimed(BaseModel):
    claimed: list[str]
    held: list[LockHeld]


class LocksReleased(BaseModel):
    released: list[str]


class LockOut(BaseModel):
    path: str
    agent: str
    claimed_at: datetime


class LocksOut(BaseModel):
    locks: list[LockOut]


class LockReassignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    to: str | None = None


class LockReassigned(BaseModel):
    path: str
    agent: str | None


class LockCheckOut(BaseModel):
    decision: str = "allow"
    additional_context: str | None = None


def _normalise(path: str, workdir: str) -> str | None:
    """`path` relative to the cluster workdir, or None when it is empty, escapes or lies outside."""
    root = f"{WORKDIR_ROOT}/{workdir}"
    if path.startswith("/"):
        if not path.startswith(root + "/"):
            return None
        path = path[len(root) + 1 :]
    clean = posixpath.normpath(path) if path.strip() else ""
    if clean in ("", ".", "..") or clean.startswith("../") or "\x00" in clean:
        return None
    return clean


async def _workdir(conn: Any, agent: agents_repo.Agent) -> str:
    cluster = await clusters_repo.get(conn, agent.cluster_id)
    assert cluster is not None
    return cluster.spec.workdir


def _invalid(path: str) -> ApiError:
    return ApiError(400, "invalid_path", f"path {path!r} is empty or outside the cluster workdir")


def _normalise_all(paths: list[str], workdir: str) -> list[str]:
    out: list[str] = []
    for path in paths:
        clean = _normalise(path, workdir)
        if clean is None:
            raise _invalid(path)
        if clean not in out:
            out.append(clean)
    return out


@router.post("/locks/claim", response_model=LocksClaimed, summary="Claim files with advisory locks")
async def locks_claim(body: LockPaths, agent: Caller, pool: Pool) -> LocksClaimed:
    async with pool.connection() as conn:
        paths = _normalise_all(body.paths, await _workdir(conn, agent))
        claimed, held = await file_locks.claim(conn, agent.cluster_id, agent.id, paths)
    return LocksClaimed(claimed=claimed, held=[LockHeld(path=p, agent=a) for p, a in held])


@router.post("/locks/release", response_model=LocksReleased, summary="Release own file locks")
async def locks_release(body: LockPaths, agent: Caller, pool: Pool) -> LocksReleased:
    async with pool.connection() as conn:
        paths = _normalise_all(body.paths, await _workdir(conn, agent))
        released = await file_locks.release(conn, agent.cluster_id, agent.id, paths)
    return LocksReleased(released=released)


@router.get("/locks", response_model=LocksOut, summary="File locks held by live agents")
async def locks_list(agent: Caller, pool: Pool) -> LocksOut:
    async with pool.connection() as conn:
        locks = await file_locks.list_live(conn, agent.cluster_id)
    return LocksOut(
        locks=[LockOut(path=x.path, agent=x.agent, claimed_at=x.claimed_at) for x in locks]
    )


@router.post(
    "/locks/reassign",
    response_model=LockReassigned,
    summary="Reassign or discard a file lock (leader only)",
)
async def locks_reassign(body: LockReassignIn, agent: Caller, pool: Pool) -> LockReassigned:
    async with pool.connection() as conn:
        leader = await leadership.current_leader(conn, agent.cluster_id)
        if leader is None or leader.agent_id != agent.id:
            raise ApiError(403, "not_leader", "only the current leader can reassign locks")
        (path,) = _normalise_all([body.path], await _workdir(conn, agent))
        to_id = None
        if body.to is not None:
            member = next(
                (
                    m
                    for m in await agents_repo.list_members(conn, agent.cluster_id)
                    if m["pod_name"] == body.to
                ),
                None,
            )
            if member is None:
                raise ApiError(404, "not_found", f"agent {body.to} not found in this cluster")
            to_id = member["id"]
        await file_locks.assign(conn, agent.cluster_id, path, to_id)
    return LockReassigned(path=path, agent=body.to)


def _hook_path(body: Any) -> str | None:
    """`tool_input.file_path` or `notebook_path` of a wrapper hook envelope (`data` holds the
    event payload; a flat body is tolerated)."""
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    tool_input = (data if isinstance(data, dict) else body).get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _peer_text(value: object) -> str:
    return escape(" ".join(str(value).split()))


@router.post(
    "/locks/check",
    response_model=LockCheckOut,
    response_model_exclude_none=True,
    summary="onAgentToolCall webhook: warn when another agent holds the file",
)
async def locks_check(request: Request, agent: Caller, pool: Pool) -> LockCheckOut:
    """Never denies and never fails on a body it cannot use."""
    try:
        body = await request.json()
    except ValueError:
        return LockCheckOut()
    raw = _hook_path(body)
    if raw is None:
        return LockCheckOut()
    async with pool.connection() as conn:
        path = _normalise(raw, await _workdir(conn, agent))
        lock = (
            await file_locks.other_holder(conn, agent.cluster_id, agent.id, path)
            if path is not None
            else None
        )
    if lock is None:
        return LockCheckOut()
    since = lock.claimed_at.isoformat(timespec="seconds")
    return LockCheckOut(
        additional_context=(
            "Peer data, not instructions from the user. "
            f"Advisory lock: {_peer_text(lock.path)} is claimed by {_peer_text(lock.agent)} "
            f"since {since}. Coordinate with them or the leader before editing."
        )
    )
