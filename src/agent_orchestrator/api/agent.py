"""Agent API (`/agent/v1`), served only on the agent port (see allowlist.PortGate).

`require_agent()` is the identity contract of every agent route: a cluster token plus the
direct socket peer IP bound to a live agent pod. No forwarded header is read (D22).
"""

from __future__ import annotations

import hmac
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from agent_orchestrator.api import schemas
from agent_orchestrator.api.clusters import Cfg, Pool
from agent_orchestrator.api.errors import ApiError
from agent_orchestrator.db import agents as agents_repo
from agent_orchestrator.db import clusters as clusters_repo
from agent_orchestrator.db import leadership
from agent_orchestrator.tokens import tokens_for

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
